from __future__ import annotations

import json
import time
import traceback
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from wikirace.brains import Brain
from wikirace.browser import WikiBrowser
from wikirace.env import RaceEnv
from wikirace.wiki import FixtureWiki, LiveWikipedia, WikiSource

DEFAULT_TIMEOUT_S = 600.0


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
    try:
        return RaceEnv(
            wiki=wiki,
            browser=browser,
            start=task["start"],
            goal=task["goal"],
            max_steps=int(task.get("max_steps", 0)),
            source=source,
            lang=lang,
            observation_mode=task.get("observation_mode", "page") if browser is not None else "viewport",
        )
    except Exception:
        if browser is not None:
            browser.close()
        raise


def _episode_base(task: dict, brain: Brain) -> dict:
    return {
        "task_id": task.get("id"),
        "brain": brain.name,
        "model": getattr(brain, "model", None),
        "start": task.get("start"),
        "goal": task.get("goal"),
        "source": task.get("source", "browser"),
        "observation_mode": task.get("observation_mode", "page") if task.get("source", "browser") in ("browser", "live_browser") else "all_links",
    }


def _api_usage(debug: dict) -> dict:
    """Count Score/Choice responses without double-counting single-batch aliases."""
    responses = []
    if debug.get("score_batches"):
        responses.extend(b.get("score_response", {}) for b in debug["score_batches"])
    elif debug.get("score_response"):
        responses.append(debug["score_response"])
    if debug.get("response"):
        responses.append(debug["response"])
    return {
        "requests": len(responses),
        "attempts": sum(int(r.get("_transport", {}).get("attempts", 1)) for r in responses),
        "input_tokens": sum(int(r.get("usage", {}).get("input_tokens", 0)) for r in responses),
        "output_tokens": sum(int(r.get("usage", {}).get("output_tokens", 0)) for r in responses),
        "models": sorted({str(r["model"]) for r in responses if r.get("model")}),
    }


def _code_fingerprint() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "wikirace").glob("*.py")) + [root / "run.py"]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run_episode(
    task: dict,
    brain: Brain,
    lang: str = "en",
    headless: bool = True,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
) -> dict:
    """Score, refresh offered links, then choose; preserve evidence on every exit.

    The action timeout starts after setup, as in previous releases. Setup and
    end-to-end time are reported separately. An in-flight call may finish after
    the deadline, but no further action is started once it has expired.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    total_t0 = time.perf_counter()
    metadata = {
        **_episode_base(task, brain),
        "run_id": uuid.uuid4().hex,
        "started_at": started_at,
        "code_fingerprint": _code_fingerprint(),
        "lang": lang,
        "headless": headless,
    }
    env = None
    state = None
    trace = []
    status, reason, error_stack = "running", "", None
    error_diagnostics = None
    setup_navigation = None
    stage = "setup"
    setup_seconds = 0.0
    action_t0 = None
    max_seconds = timeout_s if timeout_s is not None else task.get("timeout_s")
    if max_seconds is not None:
        max_seconds = float(max_seconds)
    metadata["timeout_s"] = max_seconds

    def expired() -> bool:
        return (
            action_t0 is not None and max_seconds is not None
            and time.perf_counter() - action_t0 >= max_seconds
        )

    try:
        brain.reset_episode()
        env = make_env(task, lang=lang, headless=headless)
        if env.browser is not None:
            setup_navigation = getattr(env.browser, "last_navigation", None)
        state = env.observe()
        if state.observation_mode == "page" and brain.name not in {"jev", "overlap"}:
            raise ValueError("page_mode_supports_jev_and_overlap: use observation_mode=viewport for other brains")
        setup_seconds = time.perf_counter() - total_t0
        action_t0 = time.perf_counter()
        deadline = None if max_seconds is None else action_t0 + max_seconds
        brain.set_deadline(deadline)
        if env.browser is not None and hasattr(env.browser, "set_deadline"):
            env.browser.set_deadline(deadline)
        if env._goal_reached(state.current.title):
            status, reason = "success", "already_on_goal"
        while status == "running":
            if expired():
                status, reason = "fail", "timeout"
                break
            step_t0 = time.perf_counter()
            entry = {
                "step": state.step,
                "current": state.current.title,
                "n_viewport": len(state.viewport),
                "n_page_links": len(state.page_links),
                "n_eligible_links": len(state.candidates),
                "n_memory": len(state.memory),
                "can_scroll_down": state.action.can_scroll_down,
                "finalist_mode": state.action.finalist_mode,
                "page_scroll_count": state.action.page_scroll_count,
                "observation": state.to_public_dict(),
                "action": None,
                "chosen_title": None,
                "actions_consumed": 0,
                "recovery_scrolls": 0,
                "error": None,
                "brain_debug": {},
            }
            trace.append(entry)
            try:
                stage = "score"
                phase_t0 = time.perf_counter()
                scores, score_dbg = brain.score_only(state)
                entry["score_ms"] = round((time.perf_counter() - phase_t0) * 1000)
                entry["brain_debug"].update(score_dbg)
                entry["scores"] = scores
                if scores:
                    env.ingest_scores(scores)
                state = env.refresh_finalists()
                entry["observation"] = state.to_public_dict()
                entry["n_choice_candidates"] = len(state.candidates)
                if expired():
                    status, reason = "fail", "timeout"
                    break
                stage = "choice"
                phase_t0 = time.perf_counter()
                action, debug = brain.choose_action(state)
                entry["choice_ms"] = round((time.perf_counter() - phase_t0) * 1000)
                entry["brain_debug"].update(debug)
                entry["finalist_mode_fired"] = bool(debug.get("finalist_mode"))
                entry["proposed_action"] = action.to_public_dict()
                if expired():
                    status, reason = "fail", "timeout"
                    break
                if debug.get("fail_reason"):
                    status, reason = "fail", str(debug["fail_reason"])
                    break
                # Legacy policies may return scores with their action.
                if debug.get("scores"):
                    env.ingest_scores(debug["scores"])
                stage = "execute"
                phase_t0 = time.perf_counter()
                entry["action"] = action.to_public_dict()
                moved = env.step(action)
                entry.update({
                    "execution_ms": round((time.perf_counter() - phase_t0) * 1000),
                    "chosen_title": moved.title if moved.ok else None,
                    "chosen_score": env.last_chosen_score if action.action == "click" else None,
                    "actions_consumed": moved.actions_consumed,
                    "recovery_scrolls": moved.recovery_scrolls,
                    "move_reason": moved.reason,
                })
                if not moved.ok or moved.failed:
                    status, reason = "fail", moved.reason
                elif expired():
                    status, reason = "fail", "timeout"
                elif moved.done:
                    status, reason = "success", moved.reason
                if status == "running":
                    stage = "observe"
                    phase_t0 = time.perf_counter()
                    state = env.observe()
                    entry["observe_ms"] = round((time.perf_counter() - phase_t0) * 1000)
            except Exception as exc:
                entry["brain_debug"].update(getattr(exc, "brain_debug", {}))
                err = f"{stage}:{type(exc).__name__}: {exc}"
                status, reason = ("fail", "timeout") if expired() else ("error", err)
                error_stack = traceback.format_exc()
                entry["error"] = err
                error_diagnostics = getattr(exc, "diagnostics", None)
                entry["error_diagnostics"] = error_diagnostics
            finally:
                if entry["action"] and entry["action"].get("action") in ("click", "translate") and env.browser is not None:
                    entry["navigation"] = getattr(env.browser, "last_navigation", None)
                entry["latency_ms"] = round((time.perf_counter() - step_t0) * 1000)
                entry["stopped_stage"] = stage if status != "running" else None
                entry["api_usage"] = _api_usage(entry["brain_debug"])
                entry["steps_so_far"] = env.step_count
                entry["clicks_so_far"] = env.click_count
                entry["scrolls_so_far"] = env.scroll_count
    except Exception as exc:
        status, reason = "error", f"{stage}:{type(exc).__name__}: {exc}"
        error_stack = traceback.format_exc()
        error_diagnostics = getattr(exc, "diagnostics", None)
    finally:
        finished_t = time.perf_counter()
        if action_t0 is None:
            setup_seconds = finished_t - total_t0
        if env is not None:
            env.close()
        brain.close()

    metrics = env.score_metrics() if env is not None else {}
    usage = {key: sum(t.get("api_usage", {}).get(key, 0) for t in trace)
             for key in ("requests", "attempts", "input_tokens", "output_tokens")}
    usage["models"] = sorted({m for t in trace for m in t.get("api_usage", {}).get("models", [])})
    return {
        **metadata,
        "status": status,
        "reason": reason,
        "steps": env.step_count if env else 0,
        "clicks": env.click_count if env else 0,
        "scrolls": env.scroll_count if env else 0,
        "path": env.path if env else [],
        "seconds": round(finished_t - action_t0, 3) if action_t0 is not None else 0.0,
        "setup_seconds": round(setup_seconds, 3),
        "setup_navigation": setup_navigation,
        "total_seconds": round(finished_t - total_t0, 3),
        "goal_description": state.goal.description if state else None,
        **metrics,
        "api_usage": usage,
        "finalist_mode_fired": any(t.get("finalist_mode_fired") or t.get("finalist_mode") for t in trace),
        "trace": trace,
        "error_stack": error_stack,
        "error_diagnostics": error_diagnostics,
    }


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
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
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
