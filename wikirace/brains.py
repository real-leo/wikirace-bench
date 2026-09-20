from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from wikirace.state import Action, Candidate, LinkScore, RaceState, parse_action

FINALIST_K = 5
SCORE_BATCH = 20  # API batch size; _score_viewport loops until all links scored
SCORE_LEVELS = [
    "Irrelevant or misleading; would not help reach the goal",
    "Weak / tangential connection to the goal topic",
    "Reasonable bridge; related concepts that could lead toward the goal",
    "Strong bridge; clearly on a short path toward the goal",
    "Direct or near-direct path to the goal (or is the goal itself)",
]
# Normalize Score (0..4) to 0..1 for storage/ranking.
SCORE_TOP = float(len(SCORE_LEVELS) - 1)


class Brain(ABC):
    name: str

    def reset_episode(self) -> None:
        """Clear policy memory before a new task in a shared suite."""
        self._deadline: float | None = None

    def set_deadline(self, deadline: float | None) -> None:
        self._deadline = deadline

    def close(self) -> None:
        """Release episode resources, if the policy owns any."""

    @abstractmethod
    def choose(self, state: RaceState) -> tuple[Action, dict]:
        """Return (Action, debug_payload). May score + choose in one call."""

    def score_only(self, state: RaceState) -> tuple[list[dict], dict]:
        """Score viewport links; return (score dumps for env.ingest_scores, debug)."""
        return [], {}

    def choose_action(self, state: RaceState) -> tuple[Action, dict]:
        """Choose among offered candidates without (re-)scoring.

        Default falls back to choose() for brains that do not split phases.
        """
        return self.choose(state)


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\u4e00-\u9fff]{2,}", s.lower()))


def _overlap(a: str, b: str) -> int:
    return len(_tokens(a) & _tokens(b))


def _title_key(title: str) -> str:
    return re.sub(r"\s+", " ", title.replace("_", " ").strip()).lower()


class _ScoreBookBrain(Brain):
    """Shared page/episode score memory + finalist helpers."""

    def __init__(self) -> None:
        self.reset_episode()

    def reset_episode(self) -> None:
        super().reset_episode()
        self._page_title: str | None = None
        self._page_scores: dict[str, LinkScore] = {}
        self._episode_scores: dict[str, LinkScore] = {}

    def _sync_page(self, state: RaceState) -> None:
        title = state.current.title
        if title != self._page_title:
            self._page_title = title
            self._page_scores = {}

    def _merge_scores(self, scores: list[LinkScore]) -> list[dict]:
        dumped: list[dict] = []
        for entry in scores:
            if not entry.title:
                continue
            key = _title_key(entry.title)
            if not entry.href_key:
                entry = entry.model_copy(update={"href_key": key})
            for store in (self._page_scores, self._episode_scores):
                prev = store.get(key)
                if prev is None or entry.score >= prev.score:
                    store[key] = entry
            dumped.append(entry.model_dump())
        return dumped

    def _heuristic_score(self, cand: Candidate, state: RaceState) -> float:
        goal = state.goal.title + " " + (state.goal.description or state.goal.extract)
        blob = f"{cand.title} {cand.context or cand.text} {cand.extract}"
        raw = float(_overlap(blob, goal))
        if cand.title.lower() == state.goal.title.lower():
            raw += 100.0
        if state.history and cand.title == state.history[-1]:
            raw -= 3.0
        elif cand.title in state.history:
            raw -= 1.0
        # Map rough overlap into 0..1 (cap at 8 token hits ≈ top)
        return max(0.0, min(1.0, raw / 8.0))

    def _score_viewport_heuristic(self, state: RaceState) -> list[LinkScore]:
        out: list[LinkScore] = []
        for c in (state.candidates if state.observation_mode == "page" else state.viewport):
            out.append(
                LinkScore(
                    id=c.id,
                    title=c.title,
                    context=(c.context or c.text or "")[:240],
                    score=self._heuristic_score(c, state),
                    href_key=_title_key(c.title),
                )
            )
        return out

    def _top_k_from_book(
        self, state: RaceState, k: int = FINALIST_K
    ) -> list[Candidate]:
        """Build top-K from page scores, preferring live ids.

        Prefer env-provided finalists (rebuilt after ingest+refresh). Fallback
        excludes visited/path titles *before* taking top-K.
        """
        by_title: dict[str, Candidate] = {}
        for c in list(state.memory) + list(state.viewport) + list(state.candidates):
            by_title[_title_key(c.title)] = c
        if state.finalists:
            return list(state.finalists)[:k]

        hist = {_title_key(x) for x in (state.history or state.action.path or [])}
        goal_key = _title_key(state.goal.title)
        eligible = [
            s
            for s in self._page_scores.values()
            if _title_key(s.title) == goal_key or _title_key(s.title) not in hist
        ]
        ranked = sorted(eligible, key=lambda s: s.score, reverse=True)[:k]
        finalists: list[Candidate] = []
        for sc in ranked:
            live = by_title.get(_title_key(sc.title))
            if live is not None:
                finalists.append(
                    live.model_copy(
                        update={"score": sc.score, "context": sc.context or live.context}
                    )
                )
            else:
                finalists.append(
                    Candidate(
                        id=sc.id,
                        title=sc.title,
                        context=sc.context,
                        score=sc.score,
                        position="scored_memory",
                    )
                )
        return finalists



    def score_only(self, state: RaceState) -> tuple[list[dict], dict]:
        """Score viewport into the brain book; return dumps for env.ingest_scores."""
        self._sync_page(state)
        scored, dbg = self._score_viewport_for_env(state)
        dump = self._merge_scores(scored)
        return dump, dbg

    def _score_viewport_for_env(self, state: RaceState) -> tuple[list[LinkScore], dict]:
        """Override in model brains; Overlap uses heuristic."""
        return self._score_viewport_heuristic(state), {"policy": "heuristic_score"}

    def choose_action(self, state: RaceState) -> tuple[Action, dict]:
        """Choose using already-scored state (env finalists refreshed). No re-score."""
        self._sync_page(state)
        return self._choose_after_scores(state, scores_dump=[], score_dbg={})

    def _choose_after_scores(
        self,
        state: RaceState,
        scores_dump: list[dict],
        score_dbg: dict,
    ) -> tuple[Action, dict]:
        raise NotImplementedError

class OverlapBrain(_ScoreBookBrain):
    """Heuristic scores + scroll-down / click; bottom forces top-K finalist click."""

    name = "overlap"

    def choose(self, state: RaceState) -> tuple[Action, dict]:
        scores, score_dbg = self.score_only(state)
        return self._choose_after_scores(state, scores, score_dbg)

    def _choose_after_scores(
        self,
        state: RaceState,
        scores_dump: list[dict],
        score_dbg: dict,
    ) -> tuple[Action, dict]:
        if state.observation_mode == "page":
            if not state.candidates:
                return Action(action="click", link_id=""), {"fail_reason": "page_links_empty"}
            best = max(state.candidates, key=lambda c: c.score or 0)
            return Action(action="click", link_id=best.id), {"policy": "page_heuristic"}

        finalist = bool(state.action.finalist_mode) or (
            state.source in ("browser", "live_browser")
            and not state.action.can_scroll_down
        )

        if finalist:
            top = self._top_k_from_book(state, k=state.action.finalist_k or FINALIST_K)
            if not top:
                return (
                    Action(action="click", link_id=""),
                    {
                        "policy": "finalist_empty",
                        "fail_reason": "finalist_empty",
                        "scores": scores_dump,
                        "finalist_mode": True,
                        "page_scroll_count": state.action.page_scroll_count,
                    },
                )
            best = max(top, key=lambda c: (c.score if c.score is not None else -1.0))
            return (
                Action(action="click", link_id=best.id),
                {
                    "policy": "finalist_heuristic",
                    "scores": scores_dump,
                    "finalist_mode": True,
                    "finalists": [
                        {"id": c.id, "title": c.title, "score": c.score} for c in top
                    ],
                    "chosen_score": best.score,
                    "page_scroll_count": state.action.page_scroll_count,
                },
            )

        hist = {_title_key(t) for t in (state.history or state.action.path or [])}
        goal_key = _title_key(state.goal.title)
        offered = [
            c
            for c in state.candidates
            if _title_key(c.title) not in hist or _title_key(c.title) == goal_key
        ] or list(state.candidates)
        if not offered:
            if state.action.can_scroll_down:
                return (
                    Action(action="scroll", direction="down", amount="page"),
                    {"policy": "scroll_no_candidates", "scores": scores_dump},
                )
            return (
                Action(action="scroll", direction="down", amount="page"),
                {
                    "policy": "stuck_no_candidates",
                    "fail_reason": "stuck_no_candidates",
                    "scores": scores_dump,
                },
            )

        best_id = offered[0].id
        best = -1.0
        for c in offered:
            key = _title_key(c.title)
            sc = self._page_scores.get(key)
            s = sc.score if sc is not None else self._heuristic_score(c, state)
            if s > best:
                best, best_id = s, c.id

        if best <= 0.05 and state.action.can_scroll_down and state.source in (
            "browser",
            "live_browser",
        ):
            return (
                Action(action="scroll", direction="down", amount="page"),
                {
                    "policy": "scroll_low_score",
                    "best_score": best,
                    "scores": scores_dump,
                },
            )

        return (
            Action(action="click", link_id=best_id),
            {
                "policy": "token_overlap",
                "score": best,
                "chosen_score": best,
                "link_id": best_id,
                "scores": scores_dump,
                "finalist_mode": False,
            },
        )


class JevBrain(_ScoreBookBrain):
    """TypeSafe Score (bridge relevance) + Choice (scroll-down / click / finalist).

    Each step scores visible candidates as bridges toward the goal (keep max).
    Normal mode: Choice among {SCROLL_DOWN if allowed} ∪ clickable ids.
    Finalist mode (page bottom): Choice only among top-K scored link ids.
    """

    name = "jev"

    def __init__(self, model: str = "jev-latest") -> None:
        super().__init__()
        self.model = model
        self.api_key = os.environ["TYPESAFE_API_KEY"]
        self.base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
        self._client: httpx.Client | None = None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _post(self, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        """Reuse connections; retry transient failures within the episode budget."""
        started = time.perf_counter()
        for attempt in range(3):
            remaining = None if self._deadline is None else self._deadline - time.perf_counter()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("episode_deadline_exceeded")
            request_timeout = min(timeout, remaining) if remaining is not None else timeout
            if self._client is None:
                self._client = httpx.Client(timeout=timeout)
            try:
                response = self._client.post(
                    f"{self.base}/v1/systemone",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=request_timeout,
                )
                response.raise_for_status()
                data = response.json()
                data["_transport"] = {
                    "attempts": attempt + 1,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                }
                return data
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (429, 502, 503, 504, 529) or attempt == 2:
                    raise
            except httpx.TransportError:
                self.close()
                if attempt == 2:
                    raise
            delay = 0.5 * (2 ** attempt)
            remaining = None if self._deadline is None else self._deadline - time.perf_counter()
            if remaining is not None and remaining <= delay:
                raise TimeoutError("episode_deadline_exceeded")
            time.sleep(delay)
        raise AssertionError("unreachable")

    def _score_viewport(self, state: RaceState) -> tuple[list[LinkScore], dict]:
        """Score ALL visible candidates for bridge relevance (loop SCORE_BATCH chunks)."""
        all_cands = list(state.viewport)
        if not all_cands:
            return [], {}
        scored: list[LinkScore] = []
        batch_dbgs: list[dict] = []
        for i in range(0, len(all_cands), SCORE_BATCH):
            batch = all_cands[i : i + SCORE_BATCH]
            part, dbg = self._score_viewport_batch(state, batch)
            scored.extend(part)
            batch_dbgs.append(dbg)
        out_dbg: dict = {
            "n_viewport": len(all_cands),
            "n_scored": len(scored),
            "n_batches": len(batch_dbgs),
            "score_batches": batch_dbgs,
        }
        if len(batch_dbgs) == 1:
            out_dbg.update(batch_dbgs[0])
        return scored, out_dbg

    def _score_viewport_batch(
        self, state: RaceState, to_score: list[Candidate]
    ) -> tuple[list[LinkScore], dict]:
        """Score one batch of viewport candidates."""
        if not to_score:
            return [], {}

        questions: dict[str, Any] = {}
        cand_state: dict[str, Any] = {}
        for c in to_score:
            qid = f"score_{c.id}"
            ctx = (c.context or c.text or c.extract or "")[:240]
            cand_state[c.id] = {"title": c.title, "context": ctx}
            questions[qid] = {
                "type": "score",
                "instructions": {
                    "question": (
                        "How useful is this Wikipedia link as a conceptual bridge "
                        "toward the goal article?"
                    ),
                    "focus": (
                        "Rate bridge relevance only — not writing quality. "
                        "A good bridge need not match the goal directly; related "
                        "topics that shorten the path count. "
                        f"Goal title: {state.goal.title}."
                    ),
                    "candidate_id": c.id,
                },
                "criteria": SCORE_LEVELS,
            }

        payload = {
            "model": self.model,
            "state": {
                "task": "wikirace_bridge_relevance",
                "goal": {
                    "title": state.goal.title,
                    "description": state.goal.description or state.goal.extract,
                },
                "current_page": {
                    "title": state.current.title,
                    "description": state.current.description or state.current.extract,
                },
                "path": state.action.path or state.history,
                "candidates": cand_state,
            },
            "questions": questions,
        }
        data = self._post(payload)
        answers = data.get("answers") or {}
        scored: list[LinkScore] = []
        for c in to_score:
            ans = answers.get(f"score_{c.id}") or {}
            raw = float(ans.get("score") or 0.0)
            norm = raw / SCORE_TOP if SCORE_TOP else 0.0
            scored.append(
                LinkScore(
                    id=c.id,
                    title=c.title,
                    context=(c.context or c.text or "")[:240],
                    score=round(norm, 4),
                    href_key=_title_key(c.title),
                )
            )
        return scored, {"score_request": payload, "score_response": data}

    def _score_viewport_for_env(self, state: RaceState) -> tuple[list[LinkScore], dict]:
        if state.observation_mode == "page":
            from wikirace.page_policy import score_page
            return score_page(self, state)
        return self._score_viewport(state)

    def choose(self, state: RaceState) -> tuple[Action, dict]:
        scores, score_dbg = self.score_only(state)
        return self._choose_after_scores(state, scores, score_dbg)

    def _choose_after_scores(
        self,
        state: RaceState,
        scores_dump: list[dict],
        score_dbg: dict,
    ) -> tuple[Action, dict]:
        if state.observation_mode == "page":
            from wikirace.page_policy import choose_page
            action, debug = choose_page(self, state)
            return action, {**score_dbg, **debug}
        finalist = bool(state.action.finalist_mode) or (
            state.source in ("browser", "live_browser")
            and not state.action.can_scroll_down
        )
        page_scrolls = state.action.page_scroll_count

        if finalist:
            top = self._top_k_from_book(state, k=state.action.finalist_k or FINALIST_K)
            if not top:
                return (
                    Action(action="click", link_id=""),
                    {
                        "policy": "finalist_empty",
                        "fail_reason": "finalist_empty",
                        "scores": scores_dump,
                        "finalist_mode": True,
                        "page_scroll_count": page_scrolls,
                        **score_dbg,
                    },
                )
            return self._finalist_choice(state, top, scores_dump, score_dbg, page_scrolls)

        return self._normal_choice(state, scores_dump, score_dbg)

    def _finalist_choice(
        self,
        state: RaceState,
        top: list[Candidate],
        score_dump: list[dict],
        score_dbg: dict,
        page_scrolls: int,
    ) -> tuple[Action, dict]:
        criteria: dict[str, Any] = {}
        hist = {_title_key(t) for t in (state.history or state.action.path or [])}
        goal_key = _title_key(state.goal.title)
        for c in top:
            if _title_key(c.title) in hist and _title_key(c.title) != goal_key:
                continue
            criteria[c.id] = {
                "title": c.title,
                "context": (c.context or c.text or "")[:240],
                "score": c.score,
                "what": (
                    f"Click the Wikipedia link titled {c.title!r} "
                    f"(bridge score {c.score if c.score is not None else 'n/a'})."
                ),
            }
        if not criteria:
            # All top-K were visited; fall back to raw top so we can still move.
            for c in top:
                criteria[c.id] = {
                    "title": c.title,
                    "context": (c.context or c.text or "")[:240],
                    "score": c.score,
                    "what": (
                        f"Click the Wikipedia link titled {c.title!r} "
                        f"(bridge score {c.score if c.score is not None else 'n/a'})."
                    ),
                }
        payload = {
            "model": self.model,
            "state": {
                "task": "wikirace_finalist",
                "goal": {
                    "title": state.goal.title,
                    "description": state.goal.description or state.goal.extract,
                },
                "current": {
                    "title": state.current.title,
                    "description": state.current.description or state.current.extract,
                },
                "path": state.action.path or state.history,
                "page_scroll_count": page_scrolls,
                "finalists": [
                    {
                        "id": c.id,
                        "title": c.title,
                        "context": (c.context or "")[:200],
                        "score": c.score,
                    }
                    for c in top
                ],
            },
            "questions": {
                "next": {
                    "type": "choice",
                    "instructions": {
                        "question": (
                            f"You have scrolled {page_scrolls} times on this page "
                            "and reached the bottom. Pick the single best remaining "
                            "candidate to click toward the goal."
                        ),
                        "focus": (
                            "You cannot scroll further. Choose among the top-scored "
                            "bridge candidates seen while scrolling this page. "
                            "Prefer the strongest conceptual bridge toward the goal; "
                            "avoid backtracking to pages already on the path unless "
                            "no better option exists."
                        ),
                        "goal_title": state.goal.title,
                    },
                    "criteria": criteria,
                }
            },
        }
        data = self._post(payload)
        choice = data["answers"]["next"]["choice"]
        if choice not in criteria:
            probs = (data.get("answers") or {}).get("next", {}).get("probabilities") or {}
            ranked = sorted(
                ((k, v) for k, v in probs.items() if k in criteria),
                key=lambda kv: kv[1],
                reverse=True,
            )
            choice = ranked[0][0] if ranked else top[0].id
        chosen = next((c for c in top if c.id == choice), top[0])
        return (
            Action(action="click", link_id=choice),
            {
                "policy": "finalist_choice",
                "request": payload,
                "response": data,
                "scores": score_dump,
                "finalist_mode": True,
                "finalists": [
                    {"id": c.id, "title": c.title, "score": c.score} for c in top
                ],
                "chosen_score": chosen.score,
                "page_scroll_count": page_scrolls,
                **score_dbg,
            },
        )

    def _normal_choice(
        self,
        state: RaceState,
        score_dump: list[dict],
        score_dbg: dict,
    ) -> tuple[Action, dict]:
        criteria: dict[str, Any] = {}
        hist = {_title_key(t) for t in (state.history or state.action.path or [])}
        goal_key = _title_key(state.goal.title)
        for c in state.candidates:
            if _title_key(c.title) in hist and _title_key(c.title) != goal_key:
                continue
            pos = c.position or "current_viewport"
            in_view = "current_viewport" in pos
            key = _title_key(c.title)
            sc = self._page_scores.get(key)
            score_hint = sc.score if sc is not None else c.score
            criteria[c.id] = {
                "title": c.title,
                "context": (c.context or c.text or c.extract or "")[:240],
                "position": pos,
                "score": score_hint,
                "what": (
                    f"Click the Wikipedia link titled {c.title!r}"
                    + (
                        " (currently visible)."
                        if in_view
                        else " (remembered from another viewport; executor will "
                        "scroll back, counting recovery scrolls as steps)."
                    )
                    + (
                        f" Bridge score so far: {score_hint}."
                        if score_hint is not None
                        else ""
                    )
                ),
            }

        scroll_keys: list[str] = []
        if state.source in ("browser", "live_browser") and state.action.can_scroll_down:
            criteria["SCROLL_DOWN"] = {
                "title": "(scroll down)",
                "what": "Scroll one page down to reveal more article links.",
                "not_for": (
                    "Do not scroll when a visible or remembered link is already "
                    "a reasonable bridge toward the goal. Scroll costs one step, "
                    "same as a click."
                ),
            }
            scroll_keys.append("SCROLL_DOWN")

        if not criteria:
            return (
                Action(action="scroll", direction="down", amount="page"),
                {
                    "policy": "jev_stuck_empty",
                    "fail_reason": "stuck_empty",
                    "scores": score_dump,
                    **score_dbg,
                },
            )

        unlimited = state.action.max_steps <= 0
        remaining = None if unlimited else state.action.remaining_steps
        remaining_msg = (
            "No hard step limit — minimize total actions. At page bottom "
            "SCROLL_DOWN is unavailable and you enter finalist mode."
            if unlimited
            else f"Soft remaining steps (info only, not a hard fail): {remaining}."
        )
        payload = {
            "model": self.model,
            "state": {
                "task": "wikirace",
                "goal": {
                    "title": state.goal.title,
                    "description": state.goal.description or state.goal.extract,
                },
                "current": {
                    "title": state.current.title,
                    "description": state.current.description or state.current.extract,
                },
                "path": state.action.path or state.history,
                "step": state.action.step,
                "max_steps": state.action.max_steps,
                "remaining_steps": remaining,
                "can_scroll_down": state.action.can_scroll_down,
                "page_scroll_count": state.action.page_scroll_count,
                "n_viewport": len(state.viewport),
                "n_memory": len(state.memory),
            },
            "questions": {
                "next": {
                    "type": "choice",
                    "instructions": {
                        "question": (
                            "WikiRace: choose the single next action that best "
                            "progresses toward the goal page while minimizing "
                            "total actions."
                        ),
                        "focus": (
                            "Scroll-down and click each count as one action; "
                            "minimize total actions to reach the goal. "
                            "Scores reflect bridge relevance toward the goal — "
                            "prefer higher-scored bridges when clicking. "
                            "A reasonable conceptual bridge is enough; do not wait "
                            "for a near-synonym. Only scroll if you expect clearly "
                            "more valuable candidates below. Prefer an early click "
                            "on a reasonable bridge over waiting. Avoid backtracking "
                            "to pages already on the path unless stuck. "
                            + remaining_msg
                        ),
                        "goal_title": state.goal.title,
                    },
                    "criteria": criteria,
                }
            },
        }
        data = self._post(payload)
        choice = data["answers"]["next"]["choice"]
        if choice == "SCROLL_DOWN":
            action = Action(action="scroll", direction="down", amount="page")
            chosen_score = None
        else:
            if choice not in {c.id for c in state.candidates}:
                probs = (
                    (data.get("answers") or {}).get("next", {}).get("probabilities") or {}
                )
                ranked = sorted(
                    (
                        (k, v)
                        for k, v in probs.items()
                        if k != "SCROLL_DOWN" and k in criteria
                    ),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
                choice = ranked[0][0] if ranked else (
                    state.candidates[0].id if state.candidates else "SCROLL_DOWN"
                )
                if choice == "SCROLL_DOWN":
                    action = Action(action="scroll", direction="down", amount="page")
                    return action, {
                        "request": payload,
                        "response": data,
                        "fallback": True,
                        "scores": score_dump,
                        "scroll_offered": scroll_keys,
                        **score_dbg,
                    }
            action = Action(action="click", link_id=choice)
            key = None
            for c in state.candidates:
                if c.id == choice:
                    key = _title_key(c.title)
                    break
            chosen_score = (
                self._page_scores[key].score
                if key and key in self._page_scores
                else None
            )
        return action, {
            "request": payload,
            "response": data,
            "scroll_offered": scroll_keys,
            "scores": score_dump,
            "chosen_score": chosen_score,
            "finalist_mode": False,
            **score_dbg,
        }


LLM_SYSTEM = """You are a WikiRace agent controlling a live Wikipedia browser.
Each step you see: goal, current viewport links (with sentence context), candidate
memory (union of current + previous screen), optional bridge scores, and action
state (path, step counts, can_scroll_down, page_scroll_count, finalist_mode).
max_steps/remaining_steps are soft info only (0/null = unlimited); episodes do
not fail on step count.

You must return ONE JSON action, nothing else.

Actions (EQUAL COST — each counts as one step in metrics):
1. Click an offered link: {"action":"click","link_id":"L001"}
   - link_id MUST be in the offered set. Inventing ids fails the race.
   - Clicking a remembered off-screen link makes the executor scroll back first;
     those recovery scrolls also count as steps.
2. Scroll down: {"action":"scroll","direction":"down","amount":"page"|"half"}
   - Only when can_scroll_down is true. SCROLL_UP is not available.
3. Translate (optional): {"action":"translate","target_lang":"zh"} — also one step.

Finalist mode (action.finalist_mode=true, typically at page bottom):
- You have scrolled page_scroll_count times on this page.
- Choose ONLY among the listed finalists (top-K scored bridges). No scroll.

Strategy:
- Minimize total actions to reach the goal.
- Prefer higher bridge-relevance scores when deciding what to click.
- A reasonable conceptual bridge is enough; do not wait for a near-synonym.
- Only scroll if you expect clearly more valuable candidates by scrolling.
Never invent page titles. Never explain. JSON only."""


def _parse_llm_action(content: str) -> Action:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError(f"llm_unparseable:{content[:200]}")
    return parse_action(json.loads(match.group(0)))


class OpenAICompatBrain(Brain):
    def __init__(
        self,
        name: str,
        model: str,
        api_key_env: str,
        base_url: str,
    ) -> None:
        self.name = name
        self.model = model
        self.api_key = os.environ[api_key_env]
        self.base_url = base_url.rstrip("/")

    def choose(self, state: RaceState) -> tuple[Action, dict]:
        schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["action"],
            "properties": {
                "action": {"type": "string", "enum": ["click", "scroll", "translate"]},
                "link_id": {"type": "string"},
                "direction": {"type": "string", "enum": ["down"]},
                "amount": {"type": "string", "enum": ["page", "half"]},
                "target_lang": {"type": "string"},
            },
        }
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": LLM_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(state.to_public_dict(), ensure_ascii=False),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "WikiAction",
                    "strict": False,
                    "schema": schema,
                },
            },
        }
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            if r.status_code >= 400:
                body["response_format"] = {"type": "json_object"}
                r = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            r.raise_for_status()
            data = r.json()
        content = data["choices"][0]["message"]["content"]
        action = _parse_llm_action(content)
        return action, {"request": body, "response": data}


class AnthropicBrain(Brain):
    name = "claude"

    def __init__(self, model: str = "claude-sonnet-4-5") -> None:
        self.model = model
        self.api_key = os.environ["ANTHROPIC_API_KEY"]

    def choose(self, state: RaceState) -> tuple[Action, dict]:
        payload = {
            "model": self.model,
            "max_tokens": 128,
            "temperature": 0,
            "system": LLM_SYSTEM,
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(state.to_public_dict(), ensure_ascii=False),
                }
            ],
        }
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        text = "".join(part.get("text", "") for part in data.get("content", []))
        action = _parse_llm_action(text)
        return action, {"request": payload, "response": data}


def build_brain(kind: str) -> Brain:
    kind = kind.lower()
    if kind == "overlap":
        return OverlapBrain()
    if kind == "jev":
        return JevBrain(os.environ.get("JEV_MODEL", "jev-latest"))
    if kind == "laya":
        return LayaBrain(os.environ.get("LAYA_MODEL", "convaiinnovations/laya"))
    if kind == "gpt":
        return OpenAICompatBrain(
            name="gpt",
            model=os.environ.get("GPT_MODEL", "gpt-4.1-mini"),
            api_key_env="OPENAI_API_KEY",
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
    if kind == "deepseek":
        return OpenAICompatBrain(
            name="deepseek",
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            api_key_env="DEEPSEEK_API_KEY",
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        )
    if kind == "claude":
        return AnthropicBrain(os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"))
    raise ValueError(f"unknown brain: {kind}")

# ---------------------------------------------------------------------------
# Laya (local System-1; same Score + Choice semantics as JevBrain)
# ---------------------------------------------------------------------------

_LAYA_AGENT = None


def _get_laya_agent():
    """Lazy singleton: load English checkpoint once (CPU-friendly, USE_TF=0)."""
    global _LAYA_AGENT
    if _LAYA_AGENT is not None:
        return _LAYA_AGENT
    # Must be set before importing transformers / laya (TF/abseil hang otherwise).
    os.environ.setdefault("USE_TF", "0")
    os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
    os.environ.setdefault("TRANSFORMERS_NO_FLAX", "1")
    import laya  # noqa: WPS433 — deferred so overlap/jev paths stay light

    model_id = os.environ.get("LAYA_MODEL", "convaiinnovations/laya")
    _LAYA_AGENT = laya.load(model_id)
    return _LAYA_AGENT


class LayaBrain(_ScoreBookBrain):
    """Local Laya Score (bridge relevance) + Choice (scroll-down / click / finalist).

    Mirrors JevBrain observation/action/finalist semantics for fair comparison.
    Uses the local ``laya`` package (lazy singleton, USE_TF=0).
    """

    name = "laya"

    def __init__(self, model: str | None = None) -> None:
        super().__init__()
        self.model = model or os.environ.get("LAYA_MODEL", "convaiinnovations/laya")
        self._agent = None

    def _agent_or_load(self):
        if self._agent is None:
            # Honour per-instance model id via env for the singleton loader.
            if self.model:
                os.environ.setdefault("LAYA_MODEL", self.model)
            self._agent = _get_laya_agent()
        return self._agent

    def _predict(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent_or_load()
        return agent.predict(state, questions)

    def _post(self, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        """Map System One payload to local Laya predict; shape expected by page_policy.

        Short-circuits 1-option Choice (Laya act-head topk(2) crashes on k=1).
        Caps Choice criteria to PAGE_SHORTLIST_K when page_policy offers a direct
        Choice among ≤255 links — Laya head_max_len (~192) cannot pack that many
        rendered options (Jev/TypeSafe can). Prefer goal title, then bridge_score.
        ``timeout`` is accepted for Jev/_post API compatibility and unused locally.
        """
        del timeout  # local inference; episode deadline enforced by eval loop
        if self._deadline is not None and time.perf_counter() >= self._deadline:
            raise TimeoutError("episode_deadline_exceeded")
        from wikirace.env import PAGE_SHORTLIST_K

        state = payload.get("state") or {}
        questions = payload.get("questions") or {}
        goal_title = ""
        if isinstance(state, dict):
            g = state.get("goal") or {}
            if isinstance(g, dict):
                goal_title = str(g.get("title") or "")
        goal_key = _title_key(goal_title) if goal_title else ""

        answers: dict[str, Any] = {}
        remaining: dict[str, Any] = {}
        for qid, qdef in questions.items():
            if not (
                isinstance(qdef, dict)
                and str(qdef.get("type", "")).lower() == "choice"
            ):
                remaining[qid] = qdef
                continue
            criteria = qdef.get("criteria") or {}
            if not isinstance(criteria, dict):
                remaining[qid] = qdef
                continue
            if len(criteria) == 1:
                choice = next(iter(criteria.keys()))
                answers[qid] = {
                    "type": "choice",
                    "choice": choice,
                    "short_circuit": True,
                    "probabilities": {choice: 1.0},
                }
                continue
            if len(criteria) > PAGE_SHORTLIST_K:
                def _rank(cid: str) -> tuple:
                    meta = criteria[cid] if isinstance(criteria[cid], dict) else {}
                    title = str(meta.get("title") or "")
                    is_goal = 1 if goal_key and _title_key(title) == goal_key else 0
                    score = meta.get("bridge_score")
                    if score is None:
                        score = meta.get("score")
                    try:
                        sc = float(score) if score is not None else -1.0
                    except (TypeError, ValueError):
                        sc = -1.0
                    return (is_goal, sc)

                keep_ids = sorted(criteria.keys(), key=_rank, reverse=True)[
                    :PAGE_SHORTLIST_K
                ]
                qdef = {**qdef, "criteria": {cid: criteria[cid] for cid in keep_ids}}
            remaining[qid] = qdef
        if not remaining:
            return {"answers": answers, "model": "laya-short-circuit"}
        data = self._predict(state, remaining)
        merged = dict(data.get("answers") or {})
        merged.update(answers)
        out = dict(data)
        out["answers"] = merged
        return out

    @staticmethod
    def _short_circuit_choice(criteria: dict[str, Any]) -> str | None:
        """Laya's act-head calls topk(2) and crashes on a 1-option Choice.

        When only one legal action remains, take it without calling the model
        (Jev/TypeSafe tolerates k=1; local Laya 0.3.4 does not).
        """
        if len(criteria) == 1:
            return next(iter(criteria.keys()))
        return None

    def _score_viewport(self, state: RaceState) -> tuple[list[LinkScore], dict]:
        """Score ALL visible candidates for bridge relevance (loop SCORE_BATCH chunks)."""
        all_cands = list(state.viewport)
        if not all_cands:
            return [], {}
        scored: list[LinkScore] = []
        batch_dbgs: list[dict] = []
        for i in range(0, len(all_cands), SCORE_BATCH):
            batch = all_cands[i : i + SCORE_BATCH]
            part, dbg = self._score_viewport_batch(state, batch)
            scored.extend(part)
            batch_dbgs.append(dbg)
        out_dbg: dict = {
            "n_viewport": len(all_cands),
            "n_scored": len(scored),
            "n_batches": len(batch_dbgs),
            "score_batches": batch_dbgs,
        }
        if len(batch_dbgs) == 1:
            out_dbg.update(batch_dbgs[0])
        return scored, out_dbg

    def _score_viewport_batch(
        self, state: RaceState, to_score: list[Candidate]
    ) -> tuple[list[LinkScore], dict]:
        """Score one batch of viewport candidates."""
        if not to_score:
            return [], {}

        questions: dict[str, Any] = {}
        cand_state: dict[str, Any] = {}
        for c in to_score:
            qid = f"score_{c.id}"
            ctx = (c.context or c.text or c.extract or "")[:240]
            cand_state[c.id] = {"title": c.title, "context": ctx}
            questions[qid] = {
                "type": "score",
                "instructions": {
                    "question": (
                        "How useful is this Wikipedia link as a conceptual bridge "
                        "toward the goal article?"
                    ),
                    "focus": (
                        "Rate bridge relevance only — not writing quality. "
                        "A good bridge need not match the goal directly; related "
                        "topics that shorten the path count. "
                        f"Goal title: {state.goal.title}."
                    ),
                    "candidate_id": c.id,
                },
                "criteria": SCORE_LEVELS,
            }

        score_state = {
            "task": "wikirace_bridge_relevance",
            "goal": {
                "title": state.goal.title,
                "description": state.goal.description or state.goal.extract,
            },
            "current_page": {
                "title": state.current.title,
                "description": state.current.description or state.current.extract,
            },
            "path": state.action.path or state.history,
            "candidates": cand_state,
        }
        data = self._predict(score_state, questions)
        answers = data.get("answers") or {}
        scored: list[LinkScore] = []
        for c in to_score:
            ans = answers.get(f"score_{c.id}") or {}
            raw = float(ans.get("score") or 0.0)
            norm = raw / SCORE_TOP if SCORE_TOP else 0.0
            scored.append(
                LinkScore(
                    id=c.id,
                    title=c.title,
                    context=(c.context or c.text or "")[:240],
                    score=round(norm, 4),
                    href_key=_title_key(c.title),
                )
            )
        return scored, {"score_request": {"state": score_state, "questions": questions}, "score_response": data}

    def _score_viewport_for_env(self, state: RaceState) -> tuple[list[LinkScore], dict]:
        if state.observation_mode == "page":
            from wikirace.page_policy import score_page
            return score_page(self, state)
        return self._score_viewport(state)

    def choose(self, state: RaceState) -> tuple[Action, dict]:
        scores, score_dbg = self.score_only(state)
        return self._choose_after_scores(state, scores, score_dbg)

    def _choose_after_scores(
        self,
        state: RaceState,
        scores_dump: list[dict],
        score_dbg: dict,
    ) -> tuple[Action, dict]:
        if state.observation_mode == "page":
            from wikirace.page_policy import choose_page
            action, debug = choose_page(self, state)
            return action, {**score_dbg, **debug}
        finalist = bool(state.action.finalist_mode) or (
            state.source in ("browser", "live_browser")
            and not state.action.can_scroll_down
        )
        page_scrolls = state.action.page_scroll_count

        if finalist:
            top = self._top_k_from_book(state, k=state.action.finalist_k or FINALIST_K)
            if not top:
                return (
                    Action(action="click", link_id=""),
                    {
                        "policy": "finalist_empty",
                        "fail_reason": "finalist_empty",
                        "scores": scores_dump,
                        "finalist_mode": True,
                        "page_scroll_count": page_scrolls,
                        **score_dbg,
                    },
                )
            return self._finalist_choice(state, top, scores_dump, score_dbg, page_scrolls)

        return self._normal_choice(state, scores_dump, score_dbg)

    def _finalist_choice(
        self,
        state: RaceState,
        top: list[Candidate],
        score_dump: list[dict],
        score_dbg: dict,
        page_scrolls: int,
    ) -> tuple[Action, dict]:
        criteria: dict[str, Any] = {}
        hist = {_title_key(t) for t in (state.history or state.action.path or [])}
        goal_key = _title_key(state.goal.title)
        for c in top:
            if _title_key(c.title) in hist and _title_key(c.title) != goal_key:
                continue
            criteria[c.id] = {
                "title": c.title,
                "context": (c.context or c.text or "")[:240],
                "score": c.score,
                "what": (
                    f"Click the Wikipedia link titled {c.title!r} "
                    f"(bridge score {c.score if c.score is not None else 'n/a'})."
                ),
            }
        if not criteria:
            for c in top:
                criteria[c.id] = {
                    "title": c.title,
                    "context": (c.context or c.text or "")[:240],
                    "score": c.score,
                    "what": (
                        f"Click the Wikipedia link titled {c.title!r} "
                        f"(bridge score {c.score if c.score is not None else 'n/a'})."
                    ),
                }
        choice_state = {
            "task": "wikirace_finalist",
            "goal": {
                "title": state.goal.title,
                "description": state.goal.description or state.goal.extract,
            },
            "current": {
                "title": state.current.title,
                "description": state.current.description or state.current.extract,
            },
            "path": state.action.path or state.history,
            "page_scroll_count": page_scrolls,
            "finalists": [
                {
                    "id": c.id,
                    "title": c.title,
                    "context": (c.context or "")[:200],
                    "score": c.score,
                }
                for c in top
            ],
        }
        questions = {
            "next": {
                "type": "choice",
                "instructions": {
                    "question": (
                        f"You have scrolled {page_scrolls} times on this page "
                        "and reached the bottom. Pick the single best remaining "
                        "candidate to click toward the goal."
                    ),
                    "focus": (
                        "You cannot scroll further. Choose among the top-scored "
                        "bridge candidates seen while scrolling this page. "
                        "Prefer the strongest conceptual bridge toward the goal; "
                        "avoid backtracking to pages already on the path unless "
                        "no better option exists."
                    ),
                    "goal_title": state.goal.title,
                },
                "criteria": criteria,
            }
        }
        sc = self._short_circuit_choice(criteria)
        if sc is not None:
            choice = sc
            data = {
                "answers": {"next": {"choice": choice, "short_circuit": True}},
                "model": "laya-short-circuit",
            }
        else:
            data = self._predict(choice_state, questions)
            choice = data["answers"]["next"]["choice"]
            if choice not in criteria:
                probs = (data.get("answers") or {}).get("next", {}).get("probabilities") or {}
                ranked = sorted(
                    ((k, v) for k, v in probs.items() if k in criteria),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
                choice = ranked[0][0] if ranked else top[0].id
        chosen = next((c for c in top if c.id == choice), top[0])
        return (
            Action(action="click", link_id=choice),
            {
                "policy": "finalist_choice",
                "request": {"state": choice_state, "questions": questions},
                "response": data,
                "scores": score_dump,
                "finalist_mode": True,
                "finalists": [
                    {"id": c.id, "title": c.title, "score": c.score} for c in top
                ],
                "chosen_score": chosen.score,
                "page_scroll_count": page_scrolls,
                **score_dbg,
            },
        )

    def _normal_choice(
        self,
        state: RaceState,
        score_dump: list[dict],
        score_dbg: dict,
    ) -> tuple[Action, dict]:
        criteria: dict[str, Any] = {}
        hist = {_title_key(t) for t in (state.history or state.action.path or [])}
        goal_key = _title_key(state.goal.title)
        for c in state.candidates:
            if _title_key(c.title) in hist and _title_key(c.title) != goal_key:
                continue
            pos = c.position or "current_viewport"
            in_view = "current_viewport" in pos
            key = _title_key(c.title)
            sc = self._page_scores.get(key)
            score_hint = sc.score if sc is not None else c.score
            criteria[c.id] = {
                "title": c.title,
                "context": (c.context or c.text or c.extract or "")[:240],
                "position": pos,
                "score": score_hint,
                "what": (
                    f"Click the Wikipedia link titled {c.title!r}"
                    + (
                        " (currently visible)."
                        if in_view
                        else " (remembered from another viewport; executor will "
                        "scroll back, counting recovery scrolls as steps)."
                    )
                    + (
                        f" Bridge score so far: {score_hint}."
                        if score_hint is not None
                        else ""
                    )
                ),
            }

        scroll_keys: list[str] = []
        if state.source in ("browser", "live_browser") and state.action.can_scroll_down:
            criteria["SCROLL_DOWN"] = {
                "title": "(scroll down)",
                "what": "Scroll one page down to reveal more article links.",
                "not_for": (
                    "Do not scroll when a visible or remembered link is already "
                    "a reasonable bridge toward the goal. Scroll costs one step, "
                    "same as a click."
                ),
            }
            scroll_keys.append("SCROLL_DOWN")

        if not criteria:
            return (
                Action(action="scroll", direction="down", amount="page"),
                {
                    "policy": "laya_stuck_empty",
                    "fail_reason": "stuck_empty",
                    "scores": score_dump,
                    **score_dbg,
                },
            )

        # Laya head_max_len ~192; keep Choice option count bounded (prefer high scores).
        MAX_CHOICE = 24
        if len(criteria) > MAX_CHOICE:
            scroll_crit = {k: criteria.pop(k) for k in list(scroll_keys) if k in criteria}
            ranked_ids = sorted(
                criteria.keys(),
                key=lambda cid: (
                    float(criteria[cid].get("score") or -1.0)
                    if isinstance(criteria[cid], dict)
                    else -1.0
                ),
                reverse=True,
            )[: MAX_CHOICE - len(scroll_crit)]
            criteria = {cid: criteria[cid] for cid in ranked_ids}
            criteria.update(scroll_crit)

        unlimited = state.action.max_steps <= 0
        remaining = None if unlimited else state.action.remaining_steps
        remaining_msg = (
            "No hard step limit — minimize total actions. At page bottom "
            "SCROLL_DOWN is unavailable and you enter finalist mode."
            if unlimited
            else f"Soft remaining steps (info only, not a hard fail): {remaining}."
        )
        choice_state = {
            "task": "wikirace",
            "goal": {
                "title": state.goal.title,
                "description": state.goal.description or state.goal.extract,
            },
            "current": {
                "title": state.current.title,
                "description": state.current.description or state.current.extract,
            },
            "path": state.action.path or state.history,
            "step": state.action.step,
            "max_steps": state.action.max_steps,
            "remaining_steps": remaining,
            "can_scroll_down": state.action.can_scroll_down,
            "page_scroll_count": state.action.page_scroll_count,
            "n_viewport": len(state.viewport),
            "n_memory": len(state.memory),
        }
        questions = {
            "next": {
                "type": "choice",
                "instructions": {
                    "question": (
                        "WikiRace: choose the single next action that best "
                        "progresses toward the goal page while minimizing "
                        "total actions."
                    ),
                    "focus": (
                        "Scroll-down and click each count as one action; "
                        "minimize total actions to reach the goal. "
                        "Scores reflect bridge relevance toward the goal — "
                        "prefer higher-scored bridges when clicking. "
                        "A reasonable conceptual bridge is enough; do not wait "
                        "for a near-synonym. Only scroll if you expect clearly "
                        "more valuable candidates below. Prefer an early click "
                        "on a reasonable bridge over waiting. Avoid backtracking "
                        "to pages already on the path unless stuck. "
                        + remaining_msg
                    ),
                    "goal_title": state.goal.title,
                },
                "criteria": criteria,
            }
        }
        sc = self._short_circuit_choice(criteria)
        if sc is not None:
            choice = sc
            data = {
                "answers": {"next": {"choice": choice, "short_circuit": True}},
                "model": "laya-short-circuit",
            }
        else:
            data = self._predict(choice_state, questions)
            choice = data["answers"]["next"]["choice"]
        if choice == "SCROLL_DOWN":
            action = Action(action="scroll", direction="down", amount="page")
            chosen_score = None
        else:
            if choice not in {c.id for c in state.candidates}:
                probs = (
                    (data.get("answers") or {}).get("next", {}).get("probabilities") or {}
                )
                ranked = sorted(
                    (
                        (k, v)
                        for k, v in probs.items()
                        if k != "SCROLL_DOWN" and k in criteria
                    ),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
                choice = ranked[0][0] if ranked else (
                    state.candidates[0].id if state.candidates else "SCROLL_DOWN"
                )
                if choice == "SCROLL_DOWN":
                    action = Action(action="scroll", direction="down", amount="page")
                    return action, {
                        "request": {"state": choice_state, "questions": questions},
                        "response": data,
                        "fallback": True,
                        "scores": score_dump,
                        "scroll_offered": scroll_keys,
                        **score_dbg,
                    }
            action = Action(action="click", link_id=choice)
            key = None
            for c in state.candidates:
                if c.id == choice:
                    key = _title_key(c.title)
                    break
            chosen_score = (
                self._page_scores[key].score
                if key and key in self._page_scores
                else None
            )
        return action, {
            "request": {"state": choice_state, "questions": questions},
            "response": data,
            "scroll_offered": scroll_keys,
            "scores": score_dump,
            "chosen_score": chosen_score,
            "finalist_mode": False,
            **score_dbg,
        }
