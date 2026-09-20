from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from wikirace.state import Action, RaceState, parse_action


class Brain(ABC):
    name: str

    @abstractmethod
    def choose(self, state: RaceState) -> tuple[Action, dict]:
        """Return (Action, debug_payload)."""


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\u4e00-\u9fff]{2,}", s.lower()))


def _overlap(a: str, b: str) -> int:
    return len(_tokens(a) & _tokens(b))


class OverlapBrain(Brain):
    """Heuristic: click highest overlap with goal; else scroll down if allowed."""

    name = "overlap"

    def choose(self, state: RaceState) -> tuple[Action, dict]:
        goal = state.goal.title + " " + (state.goal.description or state.goal.extract)
        offered = state.candidates
        if not offered:
            if state.action.can_scroll_down:
                return (
                    Action(action="scroll", direction="down", amount="page"),
                    {"policy": "scroll_no_candidates"},
                )
            if state.action.can_scroll_up:
                return (
                    Action(action="scroll", direction="up", amount="page"),
                    {"policy": "scroll_up_no_candidates"},
                )
            # Nowhere to go — click impossible; pick a no-op scroll that will fail
            return (
                Action(action="scroll", direction="down", amount="page"),
                {"policy": "stuck_no_candidates"},
            )

        best_id = offered[0].id
        best = -(10**9)
        for c in offered:
            blob = f"{c.title} {c.context or c.text} {c.extract}"
            s = _overlap(blob, goal)
            if c.title.lower() == state.goal.title.lower():
                s += 100
            if state.history and c.title == state.history[-1]:
                s -= 3
            elif c.title in state.history:
                s -= 1
            if s > best:
                best, best_id = s, c.id

        if best <= 0 and state.action.can_scroll_down and state.source in (
            "browser",
            "live_browser",
        ):
            return (
                Action(action="scroll", direction="down", amount="page"),
                {"policy": "scroll_low_overlap", "best_score": best},
            )

        return (
            Action(action="click", link_id=best_id),
            {"policy": "token_overlap", "score": best, "link_id": best_id},
        )


class JevBrain(Brain):
    """Typesafe Choice over offered link ids + SCROLL_DOWN/UP when physically allowed.

    The MODEL decides scroll vs click. No Noul gate. Scroll and click each count
    equally in step metrics (no hard max_steps fail).
    """

    name = "jev"

    def __init__(self, model: str = "jev-latest") -> None:
        self.model = model
        self.api_key = os.environ["TYPESAFE_API_KEY"]
        self.base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai")

    def choose(self, state: RaceState) -> tuple[Action, dict]:
        criteria: dict[str, Any] = {}
        for c in state.candidates:
            pos = c.position or "current_viewport"
            in_view = "current_viewport" in pos
            criteria[c.id] = {
                "title": c.title,
                "context": (c.context or c.text or c.extract or "")[:240],
                "position": pos,
                "what": (
                    f"Click the Wikipedia link titled {c.title!r}"
                    + (
                        " (currently visible)."
                        if in_view
                        else " (remembered from another viewport; executor will "
                        "scroll back, counting recovery scrolls as steps)."
                    )
                ),
            }

        scroll_keys: list[str] = []
        if state.source in ("browser", "live_browser"):
            if state.action.can_scroll_down:
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
            if state.action.can_scroll_up:
                criteria["SCROLL_UP"] = {
                    "title": "(scroll up)",
                    "what": "Scroll one page up.",
                    "not_for": (
                        "Do not scroll up unless you expect a better bridge above. "
                        "Scroll costs one step, same as a click."
                    ),
                }
                scroll_keys.append("SCROLL_UP")

        if not criteria:
            # Absolute stuck (no links, cannot scroll)
            action = Action(action="scroll", direction="down", amount="page")
            return action, {"policy": "jev_stuck_empty"}

        unlimited = state.action.max_steps <= 0
        remaining = None if unlimited else state.action.remaining_steps
        remaining_msg = (
            "No hard step limit — minimize total actions; at page bottom "
            "SCROLL_DOWN is unavailable so you must click or scroll up."
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
                "can_scroll_up": state.action.can_scroll_up,
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
                            "Scroll and click each count as one action; minimize "
                            "total actions to reach the goal. "
                            "A current best candidate need not match the goal "
                            "directly — a reasonable conceptual bridge is enough. "
                            "Only scroll if you expect clearly more valuable "
                            "candidates by scrolling. "
                            "Prefer an early click on a reasonable bridge over "
                            "waiting for a near-synonym with the goal. "
                            "Avoid backtracking to pages already on the path "
                            "unless stuck. "
                            "Do not oscillate scroll up/down without clicking. "
                            + remaining_msg
                        ),
                        "goal_title": state.goal.title,
                    },
                    "criteria": criteria,
                }
            },
        }
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"{self.base}/v1/systemone",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        choice = data["answers"]["next"]["choice"]
        if choice == "SCROLL_DOWN":
            action = Action(action="scroll", direction="down", amount="page")
        elif choice == "SCROLL_UP":
            action = Action(action="scroll", direction="up", amount="page")
        else:
            if choice not in {c.id for c in state.candidates}:
                probs = (data.get("answers") or {}).get("next", {}).get("probabilities") or {}
                ranked = sorted(
                    (
                        (k, v)
                        for k, v in probs.items()
                        if k not in ("SCROLL_DOWN", "SCROLL_UP") and k in criteria
                    ),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
                choice = ranked[0][0] if ranked else (
                    state.candidates[0].id if state.candidates else "SCROLL_DOWN"
                )
                if choice in ("SCROLL_DOWN", "SCROLL_UP"):
                    action = Action(
                        action="scroll",
                        direction="down" if choice == "SCROLL_DOWN" else "up",
                        amount="page",
                    )
                    return action, {"request": payload, "response": data, "fallback": True}
            action = Action(action="click", link_id=choice)
        return action, {
            "request": payload,
            "response": data,
            "scroll_offered": scroll_keys,
        }


LLM_SYSTEM = """You are a WikiRace agent controlling a live Wikipedia browser.
Each step you see: goal, current viewport links (with sentence context), candidate
memory (union of current + previous screen), and action state (path, step counts,
can_scroll_down / can_scroll_up). max_steps/remaining_steps are soft info only
(0/null = unlimited); episodes do not fail on step count.

You must return ONE JSON action, nothing else.

Actions (EQUAL COST — each counts as one step in metrics):
1. Click an offered link: {"action":"click","link_id":"L001"}
   - link_id MUST be in viewport ∪ memory ids. Inventing ids fails the race.
   - Clicking a remembered off-screen link makes the executor scroll back first;
     those recovery scrolls also count as steps.
2. Scroll: {"action":"scroll","direction":"down"|"up","amount":"page"|"half"}
   - Only when can_scroll_down / can_scroll_up is true (bottom drops SCROLL_DOWN;
     top drops SCROLL_UP). Do not oscillate up/down without clicking.
3. Translate (optional): {"action":"translate","target_lang":"zh"} — also one step.

Strategy:
- Minimize total actions to reach the goal.
- A reasonable conceptual bridge is enough; do not wait for a near-synonym.
- Only scroll if you expect clearly more valuable candidates by scrolling.
- Prefer an early click on a reasonable bridge over endless scrolling.
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
                "direction": {"type": "string", "enum": ["up", "down"]},
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
