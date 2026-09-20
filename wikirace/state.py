from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Candidate(BaseModel):
    id: str
    title: str
    href: str = ""
    text: str = ""
    # Legacy offline fixture may still carry a short extract
    extract: str = ""


class PageRef(BaseModel):
    title: str
    extract: str = ""


class RaceState(BaseModel):
    """Observation every brain sees. Candidates are viewport-visible (browser mode)
    or full-page (fixture/live API). Renumbered each observe as L001, L002, ...
    """

    task: str = "wikirace"
    goal: PageRef
    current: PageRef
    history: list[str] = Field(default_factory=list)
    step: int = 0
    max_steps: int = 12
    candidates: list[Candidate] = Field(default_factory=list)
    source: str = "browser"

    def candidate_ids(self) -> set[str]:
        return {c.id for c in self.candidates}

    def candidate_for(self, link_id: str) -> Candidate | None:
        for c in self.candidates:
            if c.id == link_id:
                return c
        return None

    def title_for(self, link_id: str) -> str | None:
        c = self.candidate_for(link_id)
        return c.title if c else None

    def to_public_dict(self) -> dict[str, Any]:
        return self.model_dump()


ActionName = Literal["click", "scroll", "translate"]
ScrollDirection = Literal["up", "down"]
ScrollAmount = Literal["page", "half"]


class Action(BaseModel):
    """Agent action schema (JSON).

    {"action":"click","link_id":"L001"}
    {"action":"scroll","direction":"down"|"up","amount":"page"|"half"}
    {"action":"translate","target_lang":"zh"}
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
    """
    if isinstance(raw, Action):
        return raw
    if not isinstance(raw, dict):
        raise ValueError(f"action_not_object:{type(raw).__name__}")
    data = dict(raw)
    if "action" not in data and "link_id" in data:
        data["action"] = "click"
    return Action.model_validate(data)
