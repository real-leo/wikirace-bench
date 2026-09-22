"""One information contract and one grouping policy for all three backends.

Only transport framing differs. Both local tokenizers determine the same groups
even when the caller is Jev; oversized inputs never get model-specific clipping.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "unified-choice-v1"
MAX_CHOICES = 255
MAX_TOKENS = 8192
HEAD_TOKENS = 7800
DESCRIPTION_CHARS = 280
CONTEXT_CHARS = 48
INSTRUCTION = (
    "Reach the specific goal article in as few link clicks as possible. "
    "Choose the exact goal if offered. Otherwise choose the single next link most "
    "likely to lead toward that specific article. Use the goal description to identify "
    "its location, organization, event series and other distinguishing facts. "
    "Shared words or a broad shared topic alone are weak evidence. Prefer concrete "
    "entity connections and relevant lists; broader hubs are useful when they offer "
    "a credible route. Title similarity is not required. Do not invent intermediate "
    "links. All options are rendered links from the current article; visited and "
    "blocked destinations have already been filtered. Choose exactly one listed option."
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def text(value):
    return " ".join(str(value or "").split())


def evidence(state):
    """Shared character limits, never per-model token clipping of information."""
    if not isinstance(state, dict):
        state = {"goal": state.goal.model_dump(), "current": state.current.model_dump(),
                 "recent_path": (state.history or state.action.path)[-6:]}
    result = {key: {"title": text(state[key].get("title")),
                    "description": text(state[key].get("description") or state[key].get("extract"))[:DESCRIPTION_CHARS]}
              for key in ("goal", "current")}
    result["recent_path"] = [text(title) for title in state.get("recent_path", [])[-6:]]
    return result


def option(candidate):
    if not isinstance(candidate, dict):
        candidate = candidate.model_dump()
    title = text(candidate["title"])
    context = text(candidate.get("context") or candidate.get("text"))
    if context == title:
        context = ""
    context = context[:CONTEXT_CHARS]
    return {"id": candidate["id"], "title": title, "context": context,
            "description": title + ("\nContext: " + context if context else "")}


@lru_cache(maxsize=1)
def contract():
    return json.loads((ROOT / "data/unified_answer_codes.json").read_text())


def decision(state, options):
    codes = contract()["codes"]
    if not 1 <= len(options) <= MAX_CHOICES:
        raise ValueError("unified_decision_requires_1_to_255_options")
    if len({o["id"] for o in options}) != len(options):
        raise ValueError("duplicate_unified_candidate_ids")
    result = {"version": VERSION, "state": state, "instruction": INSTRUCTION,
              "options": [{**o, "code": code} for o, code in zip(options, codes)]}
    result["sha256"] = digest(result)
    return result


def choice_questions(item):
    return {"next": {"type": "choice", "instructions": item["instruction"],
                     "criteria": {o["code"]: o["description"] for o in item["options"]}}}


def semif_row(item):
    return {"id": item["sha256"], "state": item["state"], "question": item["instruction"],
            "options": [{"id": o["id"], "description": o["description"]} for o in item["options"]]}


def semif_text(item, spec):
    payload = {"evidence": item["state"], "criterion": item["instruction"],
               "options": [{"letter": o["code"], "description": o["description"]} for o in item["options"]]}
    return spec["chat_prefix"] + json.dumps(payload, ensure_ascii=False) + spec["chat_suffix"]


def laya_input(item, encode, special):
    """Original Laya sequence layout, with complete options (no 48-token clipping)."""
    ids = [special["cls"]] + encode("choice question: " + item["instruction"]) + [special["sep"]]
    markers = []
    for o in item["options"]:
        markers.append(len(ids))
        ids += [special["mask"]] + encode(" " + o["code"] + ": " + o["description"])
    ids.append(special["sep"])
    head_length = len(ids)
    ids += encode(json.dumps(item["state"], ensure_ascii=False)) + [special["sep"]]
    return ids, markers, head_length


class Planner:
    def __init__(self):
        from tokenizers import Tokenizer
        self.spec = contract()
        for name, expected in self.spec["source_sha256"].items():
            if hashlib.sha256((ROOT / "models/Qwen3.5-4B" / name).read_bytes()).hexdigest() != expected:
                raise ValueError("unified_qwen_tokenizer_changed")
        self.laya = Tokenizer.from_file(str(ROOT / "models/laya-mlx/tokenizer/tokenizer.json"))
        qwen_path = ROOT / "models/Qwen3.5-4B/tokenizer.json"
        qwen_spec = json.loads(qwen_path.read_text())
        runtime_spec = json.loads((ROOT / "data/unified_qwen_pre_tokenizer.json").read_text())
        if hashlib.sha256(qwen_path.read_bytes()).hexdigest() != runtime_spec["source_tokenizer_sha256"]:
            raise ValueError("unified_qwen_runtime_spec_source_changed")
        # Transformers repairs Qwen's pre-tokenizer at load time. The raw JSON
        # alone agrees on many English inputs but diverges on e.g. Devanagari.
        # Use the same runtime splitter without requiring Transformers in every venv.
        qwen_spec["pre_tokenizer"] = runtime_spec["pre_tokenizer"]
        self.qwen = Tokenizer.from_str(json.dumps(qwen_spec))
        for tokenizer in (self.laya, self.qwen):
            tokenizer.no_padding()
            tokenizer.no_truncation()
        config = json.loads((ROOT / "models/laya-mlx/tokenizer/tokenizer_config.json").read_text())
        self.special = {}
        for key in ("cls", "sep", "mask"):
            value = config[key + "_token"]
            value = value["content"] if isinstance(value, dict) else value
            self.special[key] = self.laya.token_to_id(value)
        self.mask_text = self.laya.id_to_token(self.special["mask"])

    def encode_laya(self, value):
        # Avoid silently replacing a literal model marker in article data.
        if self.mask_text in value:
            raise ValueError("literal_laya_mask_in_unified_input")
        return self.laya.encode(value, add_special_tokens=False).ids

    def audit(self, item):
        ids, markers, head = laya_input(item, self.encode_laya, self.special)
        qwen = self.qwen.encode(semif_text(item, self.spec), add_special_tokens=False).ids
        return {"laya_tokens": len(ids), "laya_head_tokens": head, "semif_tokens": len(qwen),
                "markers": len(markers), "laya_ids_sha256": digest(ids), "semif_ids_sha256": digest(qwen),
                "fits": len(ids) <= MAX_TOKENS and head <= HEAD_TOKENS and len(qwen) <= MAX_TOKENS}

    def groups(self, state, options):
        position = 0
        while position < len(options):
            count = min(MAX_CHOICES, len(options) - position)
            item = decision(state, options[position:position + count])
            audit = self.audit(item)
            if not audit["fits"]:
                low, high = 1, count - 1
                count = 0
                while low <= high:
                    mid = (low + high) // 2
                    probe = self.audit(decision(state, options[position:position + mid]))
                    if probe["fits"]:
                        count, low = mid, mid + 1
                    else:
                        high = mid - 1
                if not count:
                    raise ValueError("complete_unified_candidate_exceeds_common_budget")
                item = decision(state, options[position:position + count])
                audit = self.audit(item)
            yield item, audit
            position += count
