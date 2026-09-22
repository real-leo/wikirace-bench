"""Offline capacity experiment; changes configuration only inside this process.

Use each backend's own venv, sequentially. Does not edit models or live adapters.
Fixtures are Jev's saved 255 finalists, not a new WikiRace accuracy evaluation.
"""
import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "reports/2026-09-21-wide-choice-fixtures.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brain", choices=["laya-mlx", "semif"], required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    import mlx.core as mx

    cases = json.loads(FIXTURES.read_text())["cases"]
    result = {"brain": args.brain, "fixture_sha256": hashlib.sha256(FIXTURES.read_bytes()).hexdigest(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "note": "Exploratory in-memory capacity override; no model retraining; controls are synthetic.",
              "measurements": [], "controls": [], "capacities": []}
    args.out.parent.mkdir(parents=True, exist_ok=True)

    def save():
        result["peak_mlx_allocation_mib"] = round(mx.get_peak_memory() / 1024**2, 2)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    started = time.perf_counter()
    if args.brain == "laya-mlx":
        from wikirace.laya_mlx_brain import LayaMLXBrain, strict_prepare, CHOICE_INSTRUCTION
        brain = LayaMLXBrain()
        brain.page_selection_policy = "grouped-all"
        agent = brain._agent_or_load()
        result["original_config"] = dict(agent.cfg)
        result["encoder_max_positions"] = agent.encoder_cfg["max_position_embeddings"]
        result["experimental_configs"] = {"small": [512, 192], "wide": [4096, 3584],
                                          "wide_with_context": [8192, 7800]}

        def native(case, entries, *, wide=False, context=False):
            agent.cfg.update(max_len=8192 if context else (4096 if wide else 512),
                             head_max_len=7800 if context else (3584 if wide else 192))
            state = brain._shared_state(case["state"])
            q = {"next": {"type": "choice", "instructions": CHOICE_INSTRUCTION,
                          "criteria": {cid: v["title"] + (
                              ". " + brain._clip(v.get("context"), 8) if context else "")
                              for cid, v in entries}}}
            audit = strict_prepare(agent, state, q)[0]
            items, _ = agent.prepare(state, q)
            assert len(items[0]["markers"]) == len(entries)
            response = agent.predict(state, q)["answers"]["next"]
            assert len(response["probabilities"]) == len(entries)
            assert all(math.isfinite(p) for p in response["probabilities"].values())
            return response["choice"], audit["input_tokens"], response["probabilities"]

        small = 8
    else:
        from wikirace.semif_brain import SemIfBrain
        from semif_phase1 import core, direct, shared
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(ROOT / "models/Qwen3.5-4B", local_files_only=True)
        labels = []
        for width in (1, 2, 3):
            for letters in itertools.product("ABCDEFGHIJKLMNOPQRSTUVWXYZ", repeat=width):
                label = "".join(letters)
                ids = tokenizer.encode(label, add_special_tokens=False)
                if len(ids) == 1 and tokenizer.decode(ids) == label:
                    labels.append(label)
            if len(labels) >= 255:
                break
        assert len(labels) >= 255
        # Keep upstream native one-token readout, while explicitly extending its code alphabet.
        core.LETTERS = direct.LETTERS = labels[:255]
        core.DIRECT_SYSTEM = shared.DIRECT_SYSTEM = (
            "Apply the supplied criterion to the supplied evidence. Choose exactly one listed option. "
            "Respond with only its uppercase code, with no explanation or reasoning.")
        result["answer_codes"] = labels[:255]
        result["numeric_0_to_254_single_token_count"] = sum(
            len(tokenizer.encode(str(i), add_special_tokens=False)) == 1 for i in range(255))
        brain = SemIfBrain()
        brain.max_tokens = 8192
        scorer = brain._agent_or_load()
        result["model_metadata"] = brain._metadata

        def native(case, entries, *, wide=False, context=False):
            row = {"id": case["task"], "state": case["state"],
                   "question": "Which link is the best next step toward the specific goal? Choose the exact goal if offered.",
                   "options": [{"id": cid, "description": v["title"] + (
                       ". Context: " + v["context"] if context and v.get("context") else "")}
                       for cid, v in entries]}
            # A larger 16K ceiling is used only for CPU capacity auditing of full-context inputs.
            ids, slots, _ = direct.encode_prompt(tokenizer, row, 16384)
            assert len(slots) == len(entries) == len(set(slots))
            if context:
                return None, len(ids), {}  # count only; no long full-context GPU run
            response = scorer.score(row)
            assert response["option_ids"] == [cid for cid, _ in entries]
            probs = dict(zip(response["option_ids"], response["probabilities"]))
            assert len(probs) == len(entries) and all(math.isfinite(p) for p in probs.values())
            return max(probs, key=probs.get), len(ids), probs

        small = 16

    result["load_seconds"] = round(time.perf_counter() - started, 3)
    print("READY", args.brain, "load_seconds", result["load_seconds"], flush=True)
    # Warm up the actual small/large shapes before the timed comparisons.
    first_entries = list(cases[0]["candidates"].items())
    native(cases[0], first_entries[:small])
    native(cases[0], first_entries, wide=True)
    save()

    def tournament(case):
        entries = list(case["candidates"].items())
        calls, maximum = 0, 0
        while len(entries) > 1:
            winners = []
            for position in range(0, len(entries), small):
                group = entries[position:position + small]
                if len(group) == 1:
                    winners.append(group[0])
                    continue
                winner, tokens, _ = native(case, group)
                maximum = max(maximum, tokens)
                calls += 1
                winners.append(next(item for item in group if item[0] == winner))
            entries = winners
        return entries[0][0], calls, maximum

    for case in cases:
        entries = list(case["candidates"].items())
        assert len(entries) == 255
        for method in [f"compact-grouped-{small}", "compact-direct-255"]:
            samples, choices, counts, tokens = [], [], [], []
            for _ in range(args.repeats):
                mx.synchronize()
                start = time.perf_counter()
                if method.startswith("compact-grouped"):
                    winner, count, length = tournament(case)
                else:
                    winner, length, _ = native(case, entries, wide=True)
                    count = 1
                mx.synchronize()
                samples.append(round(time.perf_counter() - start, 6))
                choices.append(case["candidates"][winner]["title"])
                counts.append(count)
                tokens.append(length)
            row = {"task": case["task"], "method": method, "seconds": samples,
                   "median_seconds": round(statistics.median(samples), 6), "choices": choices,
                   "calls": counts, "max_input_tokens": max(tokens), "jev_selected": case["jev_selected"]}
            result["measurements"].append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            save()
        winner, length, _ = native(case, entries, wide=True, context=True)
        result["capacities"].append({"task": case["task"], "format": "titles-and-context",
                                    "input_tokens": length, "native_gpu_run": args.brain == "laya-mlx",
                                    "selected": case["candidates"][winner]["title"] if winner else None})
        for position in (0, 127, 254):
            control = list(entries)
            control[position] = ("injected-exact-goal", {"title": case["goal"], "context": ""})
            mx.synchronize()
            start = time.perf_counter()
            winner, length, probs = native(case, control, wide=True)
            mx.synchronize()
            row = {"task": case["task"], "position_zero_based": position,
                   "input_tokens": length, "seconds": round(time.perf_counter() - start, 6),
                   "correct": winner == "injected-exact-goal", "selected": dict(control)[winner]["title"],
                   "goal_probability": probs["injected-exact-goal"]}
            grouped_winner, grouped_calls, grouped_tokens = tournament({**case, "candidates": dict(control)})
            row.update(grouped_correct=grouped_winner == "injected-exact-goal",
                       grouped_selected=dict(control)[grouped_winner]["title"],
                       grouped_calls=grouped_calls, grouped_max_tokens=grouped_tokens)
            result["controls"].append(row)
            print("CONTROL", json.dumps(row, ensure_ascii=False), flush=True)
            save()
    result["complete"] = True
    save()


if __name__ == "__main__":
    main()
