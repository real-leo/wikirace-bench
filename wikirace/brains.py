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
    """Heuristic: click highest title/text overlap with goal; else scroll down."""

    name = "overlap"

    def choose(self, state: RaceState) -> tuple[Action, dict]:
        goal = state.goal.title + " " + state.goal.extract
        if not state.candidates:
            action = Action(action="scroll", direction="down", amount="page")
            return action, {"policy": "scroll_no_candidates"}

        best_id = state.candidates[0].id
        best = -10**9
        for c in state.candidates:
            blob = f"{c.title} {c.text} {c.extract}"
            s = _overlap(blob, goal)
            # Strong bonus for exact / near title match
            if c.title.lower() == state.goal.title.lower():
                s += 100
            if state.history and c.title == state.history[-1]:
                s -= 3
            elif c.title in state.history:
                s -= 1
            if s > best:
                best, best_id = s, c.id

        # If nothing overlaps meaningfully, explore by scrolling
        if best <= 0 and state.source in ("browser", "live_browser"):
            action = Action(action="scroll", direction="down", amount="page")
            return action, {"policy": "scroll_low_overlap", "best_score": best}

        action = Action(action="click", link_id=best_id)
        return action, {"policy": "token_overlap", "score": best, "link_id": best_id}


class JevBrain(Brain):
    """Typesafe Jev Choice over click candidates; SCROLL_DOWN always offered in browser."""

    name = "jev"

    def __init__(self, model: str = "jev-latest") -> None:
        self.model = model
        self.api_key = os.environ["TYPESAFE_API_KEY"]
        self.base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai")
        # Debug-only counter; does not gate SCROLL_DOWN availability.
        self._scroll_streak = 0

    def choose(self, state: RaceState) -> tuple[Action, dict]:
        if not state.candidates:
            self._scroll_streak += 1
            action = Action(action="scroll", direction="down", amount="page")
            return action, {"policy": "jev_scroll_empty", "scroll_streak": self._scroll_streak}

        criteria: dict[str, Any] = {
            c.id: {
                "title": c.title,
                "text": (c.text or c.extract or "")[:240],
                "what": f"Click the Wikipedia link titled {c.title!r}",
            }
            for c in state.candidates
        }
        # Fair eval: always include SCROLL_DOWN in browser mode (scrolling costs a step).
        scroll_key = None
        if state.source in ("browser", "live_browser"):
            scroll_key = "SCROLL_DOWN"
            criteria[scroll_key] = {
                "title": "(scroll down)",
                "text": (
                    "Scroll one page down to reveal more article links. "
                    "Prefer a bridge click when any visible link helps toward the goal."
                ),
                "what": "Scroll down one page; do not click.",
                "not_for": "Do not scroll when any visible link is a plausible bridge toward the goal.",
            }

        payload = {
            "model": self.model,
            "state": {
                "task": "wikirace",
                "goal": state.goal.model_dump(),
                "current": state.current.model_dump(),
                "history": state.history,
                "step": state.step,
                "max_steps": state.max_steps,
                "n_candidates": len(state.candidates),
                "scroll_streak": self._scroll_streak,
            },
            "questions": {
                "next": {
                    "type": "choice",
                    "instructions": {
                        "question": (
                            "WikiRace: choose the single next action that best progresses "
                            "toward the goal page."
                        ),
                        "focus": (
                            "Prefer a conceptual bridge click over scrolling. "
                            "Avoid backtracking to pages already in history unless stuck. "
                            "SCROLL_DOWN is always available in browser mode and costs a step; "
                            "choose it only when no visible link is a plausible bridge."
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
        if scroll_key and choice == scroll_key:
            self._scroll_streak += 1
            action = Action(action="scroll", direction="down", amount="page")
        else:
            if choice not in {c.id for c in state.candidates}:
                # Unexpected option; pick highest non-scroll probability if available.
                probs = (data.get("answers") or {}).get("next", {}).get("probabilities") or {}
                ranked = sorted(
                    ((k, v) for k, v in probs.items() if k != "SCROLL_DOWN" and k in criteria),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
                choice = ranked[0][0] if ranked else state.candidates[0].id
            self._scroll_streak = 0
            action = Action(action="click", link_id=choice)
        return action, {
            "request": payload,
            "response": data,
            "scroll_streak": self._scroll_streak,
            "scroll_offered": scroll_key is not None,
        }


LLM_SYSTEM = """You are a WikiRace agent controlling a live Wikipedia browser.
Each step you ONLY see links currently visible in the browser viewport (candidates).
You must return ONE JSON action, nothing else.

Actions:
1. Click a visible link: {"action":"click","link_id":"L001"}
   - link_id MUST be one of the provided candidate ids. Inventing ids or titles fails the race.
2. Scroll the page: {"action":"scroll","direction":"down"|"up","amount":"page"|"half"}
   - Use when no good link is visible; scrolling consumes a step.
3. Translate page: {"action":"translate","target_lang":"zh"}
   - Optional; wraps current URL in Google Translate. Consumes a step.

Strategy: prefer clicks that bridge toward the goal; scroll to reveal more links; avoid loops.
Never invent page titles. Never explain. JSON only."""


def _parse_llm_action(content: str) -> Action:
    content = content.strip()
    # Strip markdown fences if any
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
