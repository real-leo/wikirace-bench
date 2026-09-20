"""Paid Jev regression probe on a controlled final viewport, not a live race.

Run against an old checkout and the current checkout with identical observations.
The old evaluator scores inside choose(); the current one scores, ingests and
refreshes before Choice. Authentication is loaded only from the specified .env.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.code_root.resolve()))
    from dotenv import load_dotenv
    from wikirace.brains import build_brain
    from wikirace.env import RaceEnv
    from wikirace.state import Candidate, LinkScore
    from wikirace.wiki import FixtureWiki

    load_dotenv(args.env)
    env = RaceEnv(start="Apollo program", goal="Moon", source="fixture", wiki=FixtureWiki())
    brain = build_brain("jev")
    old = Candidate(id="L001", title="NASA", context="United States space agency", score=0.5)
    goal = Candidate(id="L003", title="Moon", context="Earth's natural satellite; the target article.")
    old_score = LinkScore(id=old.id, title=old.title, context=old.context, score=0.5)
    state = env.observe().model_copy(update={
        "viewport": [goal], "memory": [old, goal], "candidates": [old], "finalists": [old],
        "action": env.observe().action.model_copy(update={"finalist_mode": True, "can_scroll_down": False, "page_scroll_count": 3}),
    })
    env._last_state = state
    env._curr_viewport = [goal]
    env._prev_viewport = [old]
    env.ingest_scores([old_score])
    brain._sync_page(state)
    brain._merge_scores([old_score])
    try:
        if hasattr(brain, "score_only"):
            scores, score_debug = brain.score_only(state)
            env.ingest_scores(scores)
            state = env.refresh_finalists()
            action, debug = brain.choose_action(state)
            debug = {**score_debug, **debug}
            phase = "score_ingest_refresh_choice"
        else:
            action, debug = brain.choose(state)
            scores = debug["scores"]
            env.ingest_scores(scores)
            phase = "legacy_score_inside_choice"
        moved = env.step(action)
        request = debug["request"]
        result = {
            "kind": "controlled_final_viewport_probe",
            "code_commit": subprocess.check_output(["git", "-C", str(args.code_root), "rev-parse", "HEAD"], text=True).strip(),
            "phase": phase,
            "model": debug["response"].get("model"),
            "scores": scores,
            "choice_candidates": {lid: c["title"] for lid, c in request["questions"]["next"]["criteria"].items()},
            "action": action.to_public_dict(),
            "chosen_title": moved.title,
            "reached_goal": moved.done,
            "brain_debug": debug,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "brain_debug"}, ensure_ascii=False))
    finally:
        env.close()
        if hasattr(brain, "close"):
            brain.close()


if __name__ == "__main__":
    main()
