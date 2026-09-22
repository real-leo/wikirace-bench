"""SemIf native MLX decisions over all rendered links, in bounded groups.

Group probabilities are conditional on their options: only the winner advances.
Scores from different groups are never compared as calibrated probabilities.
"""
from __future__ import annotations

import math
import os
import time
from pathlib import Path

from wikirace.brains import LayaBrain
from wikirace.page_policy import FOCUS

REVISION = "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"
ADAPTER_VERSION = "semif-mlx-page-v1-all-tournament"
INSTRUCTION = (FOCUS + " Choose the exact goal if offered. "
               "Which single link is the best next step toward the specific goal?")


class SemIfBrain(LayaBrain):
    name = "semif"
    page_selection_policy = "grouped-all"
    group_size = 16
    max_tokens = 4096

    def __init__(self, model=None, *, scorer=None, tokenizer=None, metadata=None):
        default = Path(__file__).resolve().parents[1] / "models/Qwen3.5-4B"
        super().__init__(str(model or os.environ.get("SEMIF_MODEL", default)))
        self._scorer, self._tokenizer = scorer, tokenizer
        self._metadata = metadata or {}

    def reset_episode(self):
        super().reset_episode()
        if getattr(self, "_scorer", None) is not None:
            self._scorer.cache = self._scorer.prefix = None

    def _agent_or_load(self):
        if self._scorer is None:
            import mlx.core as mx
            import mlx_lm
            from semif_phase1 import mlx_backend

            original_load = mlx_lm.load

            def load_on_cpu(*args, **kwargs):
                print("SEMIF materializing weights on CPU", flush=True)
                with mx.stream(mx.cpu):
                    result = original_load(*args, **kwargs)
                    mx.eval(result[0].parameters())
                return result

            mlx_lm.load = load_on_cpu
            try:
                model, self._tokenizer, self._metadata = mlx_backend.load_model(self.model, REVISION)
            finally:
                mlx_lm.load = original_load
            self._metadata["load_strategy"] = "CPU materialization, native MLX GPU scoring"
            self._scorer = mlx_backend.SerialPrefixScorer(
                model, self._tokenizer, self._metadata, max_tokens=self.max_tokens)
            print("SEMIF ready for native GPU scoring", flush=True)
        return self._scorer

    def _check_deadline(self):
        if self._deadline is not None and time.perf_counter() >= self._deadline:
            raise TimeoutError("episode_deadline_exceeded")

    def _validate_input(self, row):
        from semif_phase1.direct import encode_prompt

        self._agent_or_load()
        return len(encode_prompt(self._tokenizer, row, self.max_tokens)[0])

    def _group_request(self, state, group, round_index, group_index):
        row = {"id": f"round-{round_index}-group-{group_index}", "state": state,
               "question": INSTRUCTION, "options": [
                   {"id": cid, "description": value["title"] + (
                       ". Context: " + value["context"] if value.get("context") else "")}
                   for cid, value in group]}
        self._validate_input(row)  # official encoder rejects overflow; never truncates
        return row

    def _post(self, payload, timeout=60.0):
        del timeout
        if self.page_selection_policy != "grouped-all":
            raise ValueError("semif_requires_grouped_all_policy")
        if set(payload["questions"]) != {"next"} or payload["questions"]["next"]["type"] != "choice":
            raise ValueError("semif_adapter_requires_one_choice")
        criteria = payload["questions"]["next"]["criteria"]
        if not criteria:
            raise ValueError("empty_semif_choice")
        calls, rounds = [], []
        contenders = list(criteria.items())
        try:
            self._check_deadline()
            while len(contenders) > 1:
                winners, groups = [], []
                position = 0
                while position < len(contenders):
                    self._check_deadline()
                    group = contenders[position:position + self.group_size]
                    if len(group) == 1:
                        winners.append(group[0])
                        groups.append({"offered_ids": [group[0][0]], "winner": group[0][0], "bye": True})
                        position += 1
                        continue
                    while True:
                        try:
                            row = self._group_request(payload["state"], group, len(rounds), len(groups))
                            break
                        except ValueError as exc:
                            if "input tokens exceed limit" not in str(exc) or len(group) <= 2:
                                raise
                            group = group[:-1]
                    self._check_deadline()
                    started = time.perf_counter()
                    response = self._agent_or_load().score(row)
                    ids = [cid for cid, _ in group]
                    probabilities = response["probabilities"]
                    if (response["option_ids"] != ids or len(probabilities) != len(ids)
                            or any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities)):
                        raise ValueError("invalid_semif_option_scores")
                    winner = ids[max(range(len(ids)), key=probabilities.__getitem__)]
                    # Model provenance lives once in runtime instead of every native call.
                    calls.append({"candidate_ids": ids, "request": row,
                                  "response": {k: v for k, v in response.items() if k != "model"},
                                  "input_audit": [{"question": row["id"],
                                      "input_tokens": response["input_tokens"], "truncated_tokens": 0}],
                                  "milliseconds": round((time.perf_counter() - started) * 1000, 3)})
                    winners.append(next(item for item in group if item[0] == winner))
                    groups.append({"offered_ids": ids, "winner": winner})
                    position += len(group)
                rounds.append(groups)
                contenders = winners
        except Exception as exc:
            exc.brain_debug = {"mlx_partial_calls": calls, "mlx_partial_rounds": rounds,
                               "adapter_version": ADAPTER_VERSION}
            raise
        return {"model": self.model,
                "answers": {"next": {"type": "choice", "choice": contenders[0][0],
                    "policy": "token_checked_tournament", "short_circuit": len(criteria) == 1}},
                "usage": {"input_tokens": sum(c["response"]["input_tokens"] for c in calls), "output_tokens": 0},
                "runtime": {"backend": "mlx", "device": "gpu", "adapter_version": ADAPTER_VERSION,
                            "max_tokens": self.max_tokens, "group_size": self.group_size,
                            "source_model": self._metadata},
                "model_calls": calls, "choice_rounds": rounds, "offered_candidate_count": len(criteria)}

    def score_only(self, state):
        if state.observation_mode != "page":
            raise ValueError("semif supports observation_mode=page only")
        if self.page_selection_policy != "grouped-all":
            raise ValueError("semif_requires_grouped_all_policy")
        return super().score_only(state)
