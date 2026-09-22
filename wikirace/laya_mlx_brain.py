"""Budget-checked Laya MLX page adapter; large Choices use a full-coverage tournament.

The 512-token checkpoint cannot make a native 255-way decision. No candidate is
silently discarded: every offered link participates in the first round. This is
a distinct policy, not numerically equivalent to Jev's single 255-way Choice.
"""
from __future__ import annotations

import importlib.metadata
import hashlib
import json
import os
import time
from pathlib import Path

from wikirace.brains import LayaBrain

ADAPTER_VERSION = "laya-mlx-page-v2-255-tournament"
SCORE_LEVELS = ["Unrelated or conflicting", "Broad topic overlap", "Plausible hub",
                "Specific bridge to target", "Exact target or dedicated index"]
SCORE_INSTRUCTION = "How useful is this link for reaching the specific goal? Link: "
CHOICE_INSTRUCTION = (
    "Which link is the best next step toward the specific goal? "
    "Prefer its location, organization or event. Choose the exact goal if offered."
)


class InputBudgetError(ValueError):
    """A complete request does not fit the checkpoint; never infer on a partial one."""


def strict_prepare(agent, state, questions):
    """Require upstream token preparation to equal the complete untruncated input."""
    from laya_mlx.common import render_options, serialize_state

    def encode(text):
        return agent.tok(text.replace(agent.tok.mask_token, " "))["input_ids"]

    items, internal = agent.prepare(state, questions)
    state_ids = encode(serialize_state(state))
    audit = []
    for qid, item, q in zip(questions, items, internal):
        expected = [agent.tok.cls_token_id] + encode(f"{q['t']} question: {q['ins']}")
        expected.append(agent.tok.sep_token_id)
        for option in render_options(q):
            expected += [agent.tok.mask_token_id] + encode(" " + option)
        expected += [agent.tok.sep_token_id] + state_ids + [agent.tok.sep_token_id]
        if item["ids"] != expected:
            raise InputBudgetError(f"input_would_be_truncated:{qid}:full_tokens={len(expected)}")
        audit.append({"question": qid, "input_tokens": len(expected),
                      "state_tokens": len(state_ids), "truncated_tokens": 0})
    return audit


class LayaMLXBrain(LayaBrain):
    name = "laya-mlx"
    page_selection_policy = "score-255"

    @property
    def adapter_version(self):
        return ("laya-mlx-page-v3-all-tournament" if self.page_selection_policy == "grouped-all"
                else ADAPTER_VERSION)

    def __init__(self, model=None, *, agent=None):
        default = Path(__file__).resolve().parents[1] / "models/laya-mlx"
        super().__init__(str(model or os.environ.get("LAYA_MLX_MODEL", default)))
        self._agent = agent
        self._provenance = {}

    def _agent_or_load(self):
        if self._agent is None:
            import laya_mlx
            import mlx.core as mx

            if not mx.metal.is_available():
                raise RuntimeError("laya-mlx requires Apple Silicon Metal")
            mx.set_cache_limit(512 * 1024**2)
            self._agent = laya_mlx.load(self.model, device="gpu", dtype="float16", batch_size=16)
            model_dir = self._agent.model_dir
            with (model_dir / "model.safetensors").open("rb") as handle:
                self._provenance["weight_sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
            if (model_dir / "manifest.json").is_file():
                manifest = json.loads((model_dir / "manifest.json").read_text())
                self._provenance["source_revision"] = manifest.get("source_revision")
                expected = manifest.get("files", {}).get("model.safetensors", {}).get("sha256")
                if expected and self._provenance["weight_sha256"] != expected:
                    raise ValueError("laya_mlx_weight_checksum_mismatch")
        return self._agent

    def _check_deadline(self):
        if self._deadline is not None and time.perf_counter() >= self._deadline:
            raise TimeoutError("episode_deadline_exceeded")

    def _clip(self, value, tokens):
        """Explicitly shorten optional context; mandatory titles are never clipped."""
        text = str(value or "").encode("utf-8", "replace").decode("utf-8")
        tok = self._agent_or_load().tok
        if len(tok(text)["input_ids"]) <= tokens:
            return text
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if len(tok(text[:mid])["input_ids"]) <= tokens:
                low = mid
            else:
                high = mid - 1
        return text[:low]

    def _shared_state(self, state):
        goal, current = state.get("goal", {}), state.get("current", {})
        return {"goal": str(goal.get("title", "")),
                "goal_details": self._clip(goal.get("description"), 80),
                "current": str(current.get("title", "")),
                "current_details": self._clip(current.get("description"), 32),
                "recent_path": self._clip(" → ".join(state.get("recent_path", [])[-6:]), 40)}

    def _invoke(self, state, questions, calls, candidate_ids):
        self._check_deadline()
        agent = self._agent_or_load()
        audit = strict_prepare(agent, state, questions)
        start = time.perf_counter()
        response = agent.predict(state, questions)
        if set(response.get("answers", {})) != set(questions):
            raise ValueError("incomplete_laya_mlx_answers")
        calls.append({"candidate_ids": candidate_ids,
                      "request": {"state": state, "questions": questions},
                      "response": response, "input_audit": audit,
                      "milliseconds": round((time.perf_counter() - start) * 1000, 3)})
        return response["answers"]

    def _score(self, payload, calls):
        shared = self._shared_state(payload["state"])
        candidates = payload["state"]["candidates"]
        pending, ids, answers = {}, [], {}
        for qid, definition in payload["questions"].items():
            cid = definition["instructions"]["candidate_id"]
            candidate = candidates[cid]
            instruction = SCORE_INSTRUCTION + str(candidate["title"])
            context = self._clip(candidate.get("context"), 32)
            while True:
                question = {"type": "score", "instructions": instruction + (
                    ". Context: " + context if context else ""), "criteria": SCORE_LEVELS}
                try:
                    strict_prepare(self._agent_or_load(), shared, {qid: question})
                    break
                except InputBudgetError:
                    if not context:
                        raise
                    context = context[:len(context) // 2]
            pending[qid], ids = question, [*ids, cid]
            # Limit each call so the episode deadline is checked every GPU batch.
            if len(pending) == 16:
                answers.update(self._invoke(shared, pending, calls, ids))
                pending, ids = {}, []
        if pending:
            answers.update(self._invoke(shared, pending, calls, ids))
        return answers

    def _choice_request(self, shared, group):
        labels = [chr(ord("A") + i) for i in range(len(group))]
        state = {**shared, "links": {
            label: {"title": value["title"], "context": self._clip(value.get("context"), 8)}
            for label, (_, value) in zip(labels, group)}}
        question = {"next": {"type": "choice", "instructions": CHOICE_INSTRUCTION,
                             # Put meaningful text at the option markers, where
                             # Laya's scoring head reads it. Full titles also stay
                             # in state when a long title needs a short preview.
                             "criteria": {label: self._clip(value["title"], 32)
                                          for label, (_, value) in zip(labels, group)}}}
        strict_prepare(self._agent_or_load(), state, question)
        return state, question, dict(zip(labels, [cid for cid, _ in group]))

    def _choice(self, state, criteria, calls, rounds):
        if not criteria or (self.page_selection_policy != "grouped-all" and len(criteria) > 255):
            raise ValueError("laya_mlx_choice_requires_1_to_255_candidates")
        shared = self._shared_state(state)
        contenders = list(criteria.items())
        while len(contenders) > 1:
            winners, groups = [], []
            position = 0
            while position < len(contenders):
                group = contenders[position:position + 8]
                if len(group) == 1:
                    winners.append(group[0])
                    groups.append({"offered_ids": [group[0][0]], "winner": group[0][0], "bye": True})
                    position += 1
                    continue
                while True:
                    try:
                        actual_state, questions, aliases = self._choice_request(shared, group)
                        break
                    except InputBudgetError:
                        if len(group) <= 2:
                            raise
                        group = group[:-1]
                result = self._invoke(actual_state, questions, calls, [k for k, _ in group])
                answer = result["next"].get("choice")
                if answer not in aliases:
                    raise ValueError(f"invalid_laya_mlx_choice:{answer}")
                winner = aliases[answer]
                winners.append(next(entry for entry in group if entry[0] == winner))
                groups.append({"offered_ids": [k for k, _ in group], "winner": winner})
                position += len(group)
            rounds.append(groups)
            contenders = winners
        return {"type": "choice", "choice": contenders[0][0],
                "policy": "token_checked_tournament", "short_circuit": len(criteria) == 1}

    def _post(self, payload, timeout=60.0):
        del timeout
        self._check_deadline()
        calls, rounds, answers = [], [], {}
        try:
            questions = payload["questions"]
            if all(q["type"] == "score" for q in questions.values()):
                answers = self._score(payload, calls)
            else:
                for qid, question in questions.items():
                    if question["type"] != "choice":
                        raise ValueError("mixed_score_choice_payload_unsupported")
                    answers[qid] = self._choice(payload["state"], question["criteria"], calls, rounds)
        except Exception as exc:
            exc.brain_debug = {"mlx_partial_calls": calls, "mlx_partial_rounds": rounds,
                               "adapter_version": self.adapter_version}
            raise
        return {"model": self.model, "answers": answers,
                "usage": {"input_tokens": sum(c["response"].get("usage", {}).get("input_tokens", 0)
                                               for c in calls), "output_tokens": 0},
                "runtime": {"backend": "mlx", "device": "gpu", "dtype": "float16",
                            "laya_mlx_version": importlib.metadata.version("laya-mlx"),
                            "adapter_version": self.adapter_version, **self._provenance,
                            "optional_text_token_limits": {"goal_details": 80,
                                "current_details": 32, "recent_path": 40,
                                "score_context": 32, "choice_context": 8,
                                "choice_title_preview": 32},
                            "full_candidate_titles_in_input": True},
                "model_calls": calls, "choice_rounds": rounds,
                "offered_candidate_count": len(next(iter(payload["questions"].values())).get("criteria", {}))
                if rounds else None}

    def score_only(self, state):
        if state.observation_mode != "page":
            raise ValueError("laya-mlx currently supports observation_mode=page")
        return super().score_only(state)
