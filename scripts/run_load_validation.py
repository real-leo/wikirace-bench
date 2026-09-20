"""Run one hard task with compact live progress and a full final trace."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from wikirace.brains import JevBrain
from wikirace.eval import DEFAULT_TIMEOUT_S, run_episode


class ProgressJev(JevBrain):
    def choose_action(self, state):
        action, debug = super().choose_action(state)
        print(json.dumps({"step": state.step, "current": state.current.title,
                          "page_links": len(state.page_links), "choice_candidates": len(state.candidates),
                          "action": action.to_public_dict()}, ensure_ascii=False), flush=True)
        return action, debug


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", choices=["hard-dna", "hard-music", "hard-wwii"])
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--observation", choices=["page", "viewport"], default="page")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    tasks = json.loads((ROOT / "data/tasks_hard.json").read_text())
    task = next(t for t in tasks if t["id"] == args.task_id)
    task["observation_mode"] = args.observation
    row = run_episode(task, ProgressJev(), timeout_s=args.timeout)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(row, ensure_ascii=False, indent=2))
    print(json.dumps({k: row[k] for k in (
        "task_id", "status", "reason", "steps", "clicks", "scrolls", "seconds", "path"
    )}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
