from __future__ import annotations

import json
import time
import traceback
from pathlib import Path

from wikirace.brains import Brain
from wikirace.browser import WikiBrowser
from wikirace.env import RaceEnv
from wikirace.wiki import FixtureWiki, LiveWikipedia, WikiSource


def make_wiki(source: str, lang: str = "en") -> WikiSource | None:
    if source == "fixture":
        return FixtureWiki()
    if source == "live":
        return LiveWikipedia(lang=lang)
    if source in ("browser", "live_browser"):
        return None
    if source in ("baidu", "baike"):
        from wikirace.wiki import BaiduBaikeSource

        return BaiduBaikeSource()
    raise ValueError(source)


def make_env(
    task: dict,
    lang: str = "en",
    headless: bool = True,
) -> RaceEnv:
    source = task.get("source", "browser")
    wiki = make_wiki(source, lang=lang)
    browser = None
    if source in ("browser", "live_browser"):
        browser = WikiBrowser(lang=lang, headless=headless)
    return RaceEnv(
        wiki=wiki,
        browser=browser,
        start=task["start"],
        goal=task["goal"],
        max_steps=int(task.get("max_steps", 12)),
        source=source,
        lang=lang,
    )


def run_episode(
    task: dict,
    brain: Brain,
    lang: str = "en",
    headless: bool = True,
) -> dict:
    try:
        env = make_env(task, lang=lang, headless=headless)
    except Exception as exc:
        return {
            "task_id": task.get("id"),
            "brain": brain.name,
            "start": task.get("start"),
            "goal": task.get("goal"),
            "source": task.get("source", "browser"),
            "status": "error",
            "reason": f"setup:{type(exc).__name__}: {exc}",
            "steps": 0,
            "path": [],
            "seconds": 0.0,
            "trace": [],
            "error_stack": traceback.format_exc(),
        }
    try:
        state = env.observe()
        # Early success if already on goal
        if env._goal_reached(state.current.title):
            return {
                "task_id": task.get("id"),
                "brain": brain.name,
                "start": task["start"],
                "goal": task["goal"],
                "source": task.get("source", "browser"),
                "status": "success",
                "reason": "already_on_goal",
                "steps": 0,
                "path": env.path,
                "seconds": 0.0,
                "trace": [],
                "error_stack": None,
            }

        trace = []
        t0 = time.time()
        status = "running"
        reason = ""
        while True:
            step_t0 = time.time()
            try:
                action, debug = brain.choose(state)
                err = None
            except Exception as exc:
                action, debug, err = None, {}, f"{type(exc).__name__}: {exc}"
            moved = None
            action_dict = None
            if err:
                status, reason = "error", err
            else:
                assert action is not None
                action_dict = action.to_public_dict()
                moved = env.step(action)
                if not moved.ok or moved.failed:
                    status = "fail"
                    reason = moved.reason
                elif moved.done:
                    status = "success"
                    reason = moved.reason
            trace.append(
                {
                    "step": state.step,
                    "current": state.current.title,
                    "n_candidates": len(state.candidates),
                    "action": action_dict,
                    "chosen_title": None if not moved or not moved.ok else moved.title,
                    "latency_ms": int((time.time() - step_t0) * 1000),
                    "error": err,
                    "brain_debug_keys": list(debug.keys()),
                }
            )
            if status != "running":
                break
            state = env.observe()
        return {
            "task_id": task.get("id"),
            "brain": brain.name,
            "start": task["start"],
            "goal": task["goal"],
            "source": task.get("source", "browser"),
            "status": status,
            "reason": reason,
            "steps": env.step_count,
            "path": env.path,
            "seconds": round(time.time() - t0, 3),
            "trace": trace,
            "error_stack": traceback.format_exc() if status == "error" else None,
        }
    finally:
        env.close()


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    wins = sum(1 for r in rows if r["status"] == "success")
    illegal = sum(1 for r in rows if str(r.get("reason", "")).startswith("illegal_id"))
    return {
        "n": n,
        "success": wins,
        "success_rate": None if n == 0 else round(wins / n, 3),
        "illegal_id": illegal,
        "avg_steps_on_success": (
            None
            if wins == 0
            else round(sum(r["steps"] for r in rows if r["status"] == "success") / wins, 2)
        ),
    }


def run_suite(
    tasks: list[dict],
    brains: list[Brain],
    out_path: Path,
    lang: str = "en",
    headless: bool = True,
) -> list[dict]:
    rows = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for task in tasks:
            for brain in brains:
                row = run_episode(task, brain, lang=lang, headless=headless)
                rows.append(row)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                print(
                    f"{row['brain']:10} {row['task_id']} {row['status']:8} "
                    f"steps={row['steps']} {row['reason']}"
                )
    return rows
