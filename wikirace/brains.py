from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from wikirace.state import Action, Candidate, LinkScore, RaceState, parse_action

FINALIST_K = 5
SCORE_BATCH = 20  # max viewport links scored per step
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

    @abstractmethod
    def choose(self, state: RaceState) -> tuple[Action, dict]:
        """Return (Action, debug_payload)."""


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\u4e00-\u9fff]{2,}", s.lower()))


def _overlap(a: str, b: str) -> int:
    return len(_tokens(a) & _tokens(b))


def _title_key(title: str) -> str:
    return re.sub(r"\s+", " ", title.replace("_", " ").strip()).lower()


class _ScoreBookBrain(Brain):
    """Shared page/episode score memory + finalist helpers."""

    def __init__(self) -> None:
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
        for c in state.viewport:
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
        """Build top-K candidates from page scores, preferring live ids."""
        by_title: dict[str, Candidate] = {}
        for c in list(state.memory) + list(state.viewport) + list(state.candidates):
            by_title[_title_key(c.title)] = c
        # Prefer env-provided finalists if present (already ranked)
        if state.action.finalist_mode and state.finalists:
            return list(state.finalists)[:k]

        ranked = sorted(
            self._page_scores.values(), key=lambda s: s.score, reverse=True
        )[:k]
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


class OverlapBrain(_ScoreBookBrain):
    """Heuristic scores + scroll-down / click; bottom forces top-K finalist click."""

    name = "overlap"

    def choose(self, state: RaceState) -> tuple[Action, dict]:
        self._sync_page(state)
        scored = self._score_viewport_heuristic(state)
        score_dump = self._merge_scores(scored)

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
                        "scores": score_dump,
                        "finalist_mode": True,
                        "page_scroll_count": state.action.page_scroll_count,
                    },
                )
            best = max(top, key=lambda c: (c.score if c.score is not None else -1.0))
            return (
                Action(action="click", link_id=best.id),
                {
                    "policy": "finalist_heuristic",
                    "scores": score_dump,
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
                    {"policy": "scroll_no_candidates", "scores": score_dump},
                )
            return (
                Action(action="scroll", direction="down", amount="page"),
                {
                    "policy": "stuck_no_candidates",
                    "fail_reason": "stuck_no_candidates",
                    "scores": score_dump,
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
                    "scores": score_dump,
                },
            )

        return (
            Action(action="click", link_id=best_id),
            {
                "policy": "token_overlap",
                "score": best,
                "chosen_score": best,
                "link_id": best_id,
                "scores": score_dump,
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
        self.base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai")

    def _post(self, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                f"{self.base}/v1/systemone",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    def _score_viewport(self, state: RaceState) -> tuple[list[LinkScore], dict]:
        """Batch Score visible candidates for bridge relevance toward the goal."""
        to_score = list(state.viewport)[:SCORE_BATCH]
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

    def choose(self, state: RaceState) -> tuple[Action, dict]:
        self._sync_page(state)
        scored, score_dbg = self._score_viewport(state)
        score_dump = self._merge_scores(scored)

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
                        "scores": score_dump,
                        "finalist_mode": True,
                        "page_scroll_count": page_scrolls,
                        **score_dbg,
                    },
                )
            return self._finalist_choice(state, top, score_dump, score_dbg, page_scrolls)

        return self._normal_choice(state, score_dump, score_dbg)

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
