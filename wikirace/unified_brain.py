"""Opt-in matched-input WikiRace policy; legacy adapters remain reproducible."""
from __future__ import annotations

import math
import time

from wikirace.brains import Brain, build_brain
from wikirace.state import Action
from wikirace import unified_protocol as protocol


class UnifiedBrain(Brain):
    page_selection_policy = "grouped-all"

    def __init__(self, name, *, backend=None, planner=None):
        if name not in {"jev", "laya-mlx", "semif"}:
            raise ValueError("unsupported_unified_backend")
        self.name = name
        self.backend = backend or build_brain(name)
        self.model = self.backend.model
        self.planner = planner or protocol.Planner()
        self.reset_episode()
        self.native = None

    def reset_episode(self):
        super().reset_episode()
        self.backend.reset_episode()

    def set_deadline(self, deadline):
        super().set_deadline(deadline)
        self.backend.set_deadline(deadline)

    def close(self):
        self.backend.close()

    def load(self):
        if self.native is not None or self.name == "jev":
            return
        if self.name == "semif":
            from semif_phase1 import core, direct
            core.LETTERS = direct.LETTERS = self.planner.spec["codes"]
            core.DIRECT_SYSTEM = self.planner.spec["system"]
            self.backend.max_tokens = protocol.MAX_TOKENS
            self.native = self.backend._agent_or_load()
        else:
            self.native = self.backend._agent_or_load()
            self.native.cfg.update(max_len=protocol.MAX_TOKENS, head_max_len=protocol.HEAD_TOKENS)
            # Preserve the upstream framing and inference, remove only implicit
            # per-option/head/state truncation. This override is instance-local.
            def prepare(state, questions):
                items, internal = [], []
                for qid, definition in questions.items():
                    q = self.native._to_internal(definition)
                    if q["t"] != "choice":
                        raise ValueError("unified_laya_requires_choice")
                    item = {"state": state, "instruction": q["ins"],
                            "options": [{"code": k, "description": v} for k, v in q["crit"].items()]}
                    ids, markers, head = protocol.laya_input(item, self.planner.encode_laya, self.planner.special)
                    if len(ids) > protocol.MAX_TOKENS or head > protocol.HEAD_TOKENS:
                        raise ValueError("unified_laya_input_exceeds_budget")
                    items.append({"ids": ids, "markers": markers, "qtype": 0})
                    internal.append(q)
                return items, internal
            self.native.prepare = prepare

    def score_only(self, state):
        return [], {"prompt_version": protocol.VERSION, "page_policy": "unified-all-255",
                    "n_eligible_links": len(state.candidates)}

    def infer(self, item, audit):
        if self._deadline is not None and time.perf_counter() >= self._deadline:
            raise TimeoutError("episode_deadline_exceeded")
        if not audit["fits"]:
            raise ValueError("unified_input_exceeds_budget")
        self.load()
        started = time.perf_counter()
        code_to_id = {o["code"]: o["id"] for o in item["options"]}
        if self.name == "semif":
            from semif_phase1.direct import encode_prompt
            request = protocol.semif_row(item)
            ids, slots, _ = encode_prompt(self.backend._tokenizer, request, protocol.MAX_TOKENS)
            if protocol.digest(ids) != audit["semif_ids_sha256"] or len(slots) != len(code_to_id):
                raise ValueError("semif_actual_input_differs_from_shared_contract")
            native_response = self.native.score(request)
            ids = [o["id"] for o in item["options"]]
            probs = native_response["probabilities"]
            if (native_response["option_ids"] != ids or len(probs) != len(ids)
                    or any(not math.isfinite(p) or not 0 <= p <= 1 for p in probs)):
                raise ValueError("invalid_unified_semif_response")
            winner = ids[max(range(len(ids)), key=probs.__getitem__)]
            response = {k: v for k, v in native_response.items() if k != "model"}
            response.update(model=self.model, usage={"input_tokens": native_response["input_tokens"],
                                                    "output_tokens": 0})
        else:
            request = {"model": self.model, "state": item["state"],
                       "questions": protocol.choice_questions(item)}
            if self.name == "jev":
                try:
                    response = self.backend._post(request)
                except Exception as exc:
                    error_response = getattr(exc, "response", None)
                    exc.brain_debug = {"unified_failed_request": {
                        "canonical": item, "request": request, "input_audit": audit,
                        "status_code": getattr(error_response, "status_code", None),
                        "response_body": getattr(error_response, "text", "")[:10000]}}
                    raise
            else:
                from wikirace.laya_mlx_brain import strict_prepare
                strict_prepare(self.native, item["state"], request["questions"])
                prepared, _ = self.native.prepare(item["state"], request["questions"])
                if protocol.digest(prepared[0]["ids"]) != audit["laya_ids_sha256"]:
                    raise ValueError("laya_actual_input_differs_from_shared_contract")
                response = self.native.predict(item["state"], request["questions"])
            choice = response.get("answers", {}).get("next", {}).get("choice")
            if choice not in code_to_id:
                raise ValueError(f"invalid_unified_choice:{choice}")
            if self.name == "laya-mlx":
                probabilities = response["answers"]["next"]["probabilities"]
                if set(probabilities) != set(code_to_id) or any(not math.isfinite(p) for p in probabilities.values()):
                    raise ValueError("invalid_unified_laya_probabilities")
            winner = code_to_id[choice]
        return winner, {"canonical": item, "input_audit": audit, "request": request,
                        "response": response, "winner": winner,
                        "milliseconds": round((time.perf_counter() - started) * 1000, 3)}

    def choose(self, state):
        return self.choose_action(state)

    def choose_action(self, state):
        if state.observation_mode != "page":
            raise ValueError("unified_protocol_requires_page_mode")
        if not state.candidates:
            return Action(action="click", link_id=""), {"fail_reason": "page_links_empty"}
        shared = protocol.evidence(state)
        contenders = [protocol.option(c) for c in state.candidates]
        calls, rounds = [], []
        try:
            while len(contenders) > 1:
                winners, groups = [], []
                for item, audit in self.planner.groups(shared, contenders):
                    if len(item["options"]) == 1:
                        winner = item["options"][0]["id"]
                    else:
                        winner, call = self.infer(item, audit)
                        calls.append(call)
                    winners.append(next(o for o in contenders if o["id"] == winner))
                    groups.append({"ids": [o["id"] for o in item["options"]], "winner": winner,
                                   "canonical_sha256": item["sha256"], "bye": len(item["options"]) == 1})
                if len(winners) >= len(contenders):
                    raise ValueError("unified_budget_cannot_compare_two_candidates")
                rounds.append(groups)
                contenders = winners
        except Exception as exc:
            exc.brain_debug = {**getattr(exc, "brain_debug", {}), "unified_calls": calls,
                               "unified_rounds": rounds, "prompt_version": protocol.VERSION}
            raise
        return Action(action="click", link_id=contenders[0]["id"]), {
            "prompt_version": protocol.VERSION, "unified_calls": calls, "unified_rounds": rounds,
            "evidence_sha256": protocol.digest(shared), "finalist_mode": False,
            "choice_candidate_count": len(state.candidates)}
