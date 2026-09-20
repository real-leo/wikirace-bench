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
        max_steps=int(task.get("max_steps", 0)),
        source=source,
        lang=lang,
    )


def _episode_base(task: dict, brain: Brain) -> dict:
    return {
        "task_id": task.get("id"),
        "brain": brain.name,
        "start": task.get("start"),
        "goal": task.get("goal"),
        "source": task.get("source", "browser"),
    }


def run_episode(
    task: dict,
    brain: Brain,
    lang: str = "en",
    headless: bool = True,
    timeout_s: float | None = 120.0,
) -> dict:
    """Run one race.

    Result fields:
      status, path, steps (total actions), clicks, scrolls, seconds, reason
    """
    try:
        env = make_env(task, lang=lang, headless=headless)
    except Exception as exc:
        return {
            **_episode_base(task, brain),
            "status": "error",
            "reason": f"setup:{type(exc).__name__}: {exc}",
            "steps": 0,
            "clicks": 0,
            "scrolls": 0,
            "path": [],
            "seconds": 0.0,
            "trace": [],
            "error_stack": traceback.format_exc(),
        }
    try:
        state = env.observe()
        if env._goal_reached(state.current.title):
            return {
                **_episode_base(task, brain),
                "start": task["start"],
                "goal": task["goal"],
                "status": "success",
                "reason": "already_on_goal",
                "steps": 0,
                "clicks": 0,
                "scrolls": 0,
                "path": env.path,
                "seconds": 0.0,
                "trace": [],
                "error_stack": None,
            }

        trace = []
        t0 = time.time()
        status = "running"
        reason = ""
        max_seconds = timeout_s
        if max_seconds is None and task.get("timeout_s") is not None:
            max_seconds = float(task["timeout_s"])
        elif max_seconds is None:
            max_seconds = None
        else:
            max_seconds = float(max_seconds)

        while True:
            if max_seconds is not None and (time.time() - t0) >= max_seconds:
                status, reason = "fail", "timeout"
                trace.append(
                    {
                        "step": state.step,
                        "current": state.current.title,
                        "n_viewport": len(state.viewport),
                        "n_memory": len(state.memory),
                        "can_scroll_down": state.action.can_scroll_down,
                        "can_scroll_up": state.action.can_scroll_up,
                        "action": None,
                        "chosen_title": None,
                        "latency_ms": 0,
                        "error": "timeout",
                        "brain_debug_keys": [],
                    }
                )
                break

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
                    "n_viewport": len(state.viewport),
                    "n_memory": len(state.memory),
                    "can_scroll_down": state.action.can_scroll_down,
                    "can_scroll_up": state.action.can_scroll_up,
                    "action": action_dict,
                    "chosen_title": None if not moved or not moved.ok else moved.title,
                    "latency_ms": int((time.time() - step_t0) * 1000),
                    "error": err,
                    "brain_debug_keys": list(debug.keys()),
                    "recovery_scrolls": getattr(moved, "recovery_scrolls", 0) if moved else 0,
                    "actions_consumed": getattr(moved, "actions_consumed", 1) if moved else 0,
                    "steps_so_far": env.step_count,
                    "clicks_so_far": env.click_count,
                    "scrolls_so_far": env.scroll_count,
                }
            )
            if status != "running":
                break
            state = env.observe()
        return {
            **_episode_base(task, brain),
            "start": task["start"],
            "goal": task["goal"],
            "status": status,
            "reason": reason,
            "steps": env.step_count,
            "clicks": env.click_count,
            "scrolls": env.scroll_count,
            "path": env.path,
            "seconds": round(time.time() - t0, 3),
            "trace": trace,
            "error_stack": traceback.format_exc() if status == "error" else None,
        }
    finally:
        env.close()


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    wins = [r for r in rows if r["status"] == "success"]
    n_wins = len(wins)
    illegal = sum(1 for r in rows if str(r.get("reason", "")).startswith("illegal_id"))
    return {
        "n": n,
        "success": n_wins,
        "success_rate": None if n == 0 else round(n_wins / n, 3),
        "illegal_id": illegal,
        "avg_steps_on_success": (
            None
            if n_wins == 0
            else round(sum(r["steps"] for r in wins) / n_wins, 2)
        ),
        "avg_clicks": (
            None
            if n == 0
            else round(sum(int(r.get("clicks") or 0) for r in rows) / n, 2)
        ),
        "avg_scrolls": (
            None
            if n == 0
            else round(sum(int(r.get("scrolls") or 0) for r in rows) / n, 2)
        ),
        "avg_seconds": (
            None
            if n == 0
            else round(sum(float(r.get("seconds") or 0) for r in rows) / n, 3)
        ),
    }


def run_suite(
    tasks: list[dict],
    brains: list[Brain],
    out_path: Path,
    lang: str = "en",
    headless: bool = True,
    timeout_s: float | None = 120.0,
) -> list[dict]:
    rows = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for task in tasks:
            for brain in brains:
                row = run_episode(
                    task, brain, lang=lang, headless=headless, timeout_s=timeout_s
                )
                rows.append(row)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                print(
                    f"{row['brain']:10} {row['task_id']} {row['status']:8} "
                    f"steps={row['steps']} clicks={row.get('clicks', 0)} "
                    f"scrolls={row.get('scrolls', 0)} "
                    f"sec={row.get('seconds')} {row['reason']}"
                )
    return rows
