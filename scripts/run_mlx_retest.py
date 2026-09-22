"""Run page retests with live progress and create-only episode files."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from wikirace.brains import build_brain
from wikirace.eval import run_episode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brain", default="laya-mlx", choices=["laya-mlx", "semif", "jev"])
    parser.add_argument("--page-policy", choices=["score-255", "grouped-all"])
    parser.add_argument("--tasks", type=Path, default=ROOT / "data/tasks_hard.json")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    args.out_dir.mkdir(parents=True, exist_ok=False)
    brain = build_brain(args.brain)
    if args.page_policy:
        if args.brain == "jev" and args.page_policy != "score-255":
            raise ValueError("Jev uses its native score-255 policy")
        brain.page_selection_policy = args.page_policy
    start = time.perf_counter()
    if args.brain in {"laya-mlx", "semif"}:
        native = brain._agent_or_load()  # isolate local model loading from action budget
        method = "predict" if args.brain == "laya-mlx" else "score"
        original_native = getattr(native, method)
        native_count = 0

        def native_progress(*a, **kw):
            nonlocal native_count
            result = original_native(*a, **kw)
            native_count += 1
            if native_count % 32 == 0:
                print(f"NATIVE completed_calls={native_count}", flush=True)
            return result

        setattr(native, method, native_progress)
    print(f"READY brain={brain.name} load_seconds={time.perf_counter() - start:.3f}", flush=True)
    original_score, original_choose = brain.score_only, brain.choose_action

    def score(state):
        print(f"SCORE step={state.step} page={state.current.title!r} links={len(state.candidates)}", flush=True)
        return original_score(state)

    def choose(state):
        start = time.perf_counter()
        result = original_choose(state)
        title = state.title_for(result[0].link_id)
        print(f"CHOICE page={state.current.title!r} offered={len(state.candidates)} "
              f"next={title!r} seconds={time.perf_counter()-start:.3f}", flush=True)
        return result

    brain.score_only, brain.choose_action = score, choose
    for task in json.loads(args.tasks.read_text()):
        task = {**task, "source": "browser", "observation_mode": "page"}
        with (args.out_dir / (task["id"] + ".progress.jsonl")).open("x", encoding="utf-8") as progress:
            def record_progress(entry, status):
                snapshot = {"task_id": task["id"], **status,
                            **{k: entry.get(k) for k in ["step", "current", "chosen_title",
                                "clicks_so_far", "elapsed_seconds", "action_elapsed_seconds",
                                "n_choice_candidates", "error"]}}
                progress.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
                progress.flush()
            row = run_episode(task, brain, timeout_s=args.timeout, progress_callback=record_progress)
        for suffix, value in [(".json", row), (".summary.json", {k:v for k,v in row.items() if k != "trace"})]:
            path = args.out_dir / (task["id"] + suffix)
            with path.open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
        print(f"RESULT task={task['id']} status={row['status']} reason={row['reason']} "
              f"clicks={row['clicks']} seconds={row['seconds']} path={row['path']}", flush=True)


if __name__ == "__main__":
    main()
