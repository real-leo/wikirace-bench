from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Candidate(BaseModel):
    """Public candidate offered to the brain (no real href)."""

    id: str
    title: str
    context: str = ""  # short sentence context around the link
    position: str = "current_viewport"  # e.g. current_viewport | prev_viewport | scrollY=1234
    # Legacy / offline fields (optional)
    text: str = ""
    extract: str = ""


class PageRef(BaseModel):
    title: str
    description: str = ""  # short goal / page description
    # Back-compat alias used in older dumps
    extract: str = ""

    def model_post_init(self, __context: Any) -> None:
        if self.description and not self.extract:
            object.__setattr__(self, "extract", self.description)
        elif self.extract and not self.description:
            object.__setattr__(self, "description", self.extract)


class ActionState(BaseModel):
    path: list[str] = Field(default_factory=list)
    step: int = 0
    # 0 = unlimited (soft info only; never terminates the episode)
    max_steps: int = 0
    # None when unlimited; otherwise soft remaining for the model
    remaining_steps: int | None = None
    can_scroll_down: bool = True
    can_scroll_up: bool = False


class RaceState(BaseModel):
    """Observation every brain sees each step.

    - viewport: links visible in the current browser viewport (main content)
    - memory: union of current + previous screen links (v1, no extra model filter)
    - offered click ids = viewport ∪ memory
    Real hrefs stay in the executor only.
    """

    task: str = "wikirace"
    goal: PageRef
    current: PageRef
    viewport: list[Candidate] = Field(default_factory=list)
    memory: list[Candidate] = Field(default_factory=list)
    action: ActionState = Field(default_factory=ActionState)
    source: str = "browser"

    # Convenience mirrors (also in action) for older brains / dumps
    history: list[str] = Field(default_factory=list)
    step: int = 0
    max_steps: int = 0  # 0 = unlimited
    candidates: list[Candidate] = Field(default_factory=list)  # = offered set

    def offered_ids(self) -> set[str]:
        return {c.id for c in self.candidates}

    def candidate_ids(self) -> set[str]:
        return self.offered_ids()

    def candidate_for(self, link_id: str) -> Candidate | None:
        for c in self.candidates:
            if c.id == link_id:
                return c
        return None

    def title_for(self, link_id: str) -> str | None:
        c = self.candidate_for(link_id)
        return c.title if c else None

    def to_public_dict(self) -> dict[str, Any]:
        """Observation payload for LLM / Jev (no href)."""
        return {
            "task": self.task,
            "goal": {
                "title": self.goal.title,
                "description": self.goal.description or self.goal.extract,
            },
            "current": {
                "title": self.current.title,
                "description": self.current.description or self.current.extract,
            },
            "viewport": [
                {
                    "id": c.id,
                    "title": c.title,
                    "context": c.context or c.text or c.extract,
                    "position": c.position,
                }
                for c in self.viewport
            ],
            "memory": [
                {
                    "id": c.id,
                    "title": c.title,
                    "context": c.context or c.text or c.extract,
                    "position": c.position,
                }
                for c in self.memory
            ],
            "action": {
                "path": self.action.path,
                "step": self.action.step,
                "max_steps": self.action.max_steps,
                "remaining_steps": (
                    None
                    if self.action.max_steps <= 0
                    else self.action.remaining_steps
                ),
                "can_scroll_down": self.action.can_scroll_down,
                "can_scroll_up": self.action.can_scroll_up,
            },
            "source": self.source,
        }


ActionName = Literal["click", "scroll", "translate"]
ScrollDirection = Literal["up", "down"]
ScrollAmount = Literal["page", "half"]


class Action(BaseModel):
    """Agent action schema (JSON).

    {"action":"click","link_id":"L001"}
    {"action":"scroll","direction":"down"|"up","amount":"page"|"half"}
    {"action":"translate","target_lang":"zh"}  # optional; also costs one step
    """

    action: ActionName
    link_id: str | None = None
    direction: ScrollDirection | None = None
    amount: ScrollAmount | None = "page"
    target_lang: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _default_amount(cls, v: Any) -> Any:
        return v if v is not None else "page"

    def to_public_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"action": self.action}
        if self.action == "click":
            d["link_id"] = self.link_id
        elif self.action == "scroll":
            d["direction"] = self.direction
            d["amount"] = self.amount or "page"
        elif self.action == "translate":
            d["target_lang"] = self.target_lang
        return d


def parse_action(raw: Any) -> Action:
    """Parse brain output into Action. Accepts dict or JSON-like objects.

    Legacy: bare {"link_id":"L001"} is treated as click.
    SCROLL_DOWN / SCROLL_UP choice keys map to scroll actions.
    """
    if isinstance(raw, Action):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if s == "SCROLL_DOWN":
            return Action(action="scroll", direction="down", amount="page")
        if s == "SCROLL_UP":
            return Action(action="scroll", direction="up", amount="page")
        return Action(action="click", link_id=s)
    if not isinstance(raw, dict):
        raise ValueError(f"action_not_object:{type(raw).__name__}")
    data = dict(raw)
    if "action" not in data and "link_id" in data:
        data["action"] = "click"
    return Action.model_validate(data)
