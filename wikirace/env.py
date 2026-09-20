from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from wikirace.browser import WikiBrowser, normalize_wiki_title
from wikirace.state import Action, Candidate, PageRef, RaceState, parse_action
from wikirace.wiki import WikiSource

if TYPE_CHECKING:
    pass

MAX_CANDIDATES = 255
EXTRACT_CACHE_CHARS = 180


@dataclass
class StepResult:
    ok: bool
    title: str | None
    reason: str = ""
    done: bool = False
    failed: bool = False
    action: str = ""


@dataclass
class RaceEnv:
    """WikiRace environment.

    Primary mode: source=browser (DrissionPage, viewport-visible links).
    Offline: fixture / live MediaWiki API (full-page links, no scroll/translate).
    """

    start: str
    goal: str
    max_steps: int = 12
    source: str = "browser"
    wiki: WikiSource | None = None
    browser: WikiBrowser | None = None
    lang: str = "en"
    current: str = ""
    path: list[str] = field(default_factory=list)
    step_count: int = 0
    extracts: dict[str, str] = field(default_factory=dict)
    link_cache: dict[str, list[str]] = field(default_factory=dict)
    _last_state: RaceState | None = None

    def __post_init__(self) -> None:
        self.reset()

    @property
    def is_browser(self) -> bool:
        return self.source in ("browser", "live_browser") and self.browser is not None

    def reset(self) -> RaceState:
        self.current = self.start
        self.path = [self.start]
        self.step_count = 0
        self._last_state = None
        if self.is_browser:
            assert self.browser is not None
            self.browser.open_article(self.start)
            meta = self.browser.current_meta()
            if meta["title"]:
                self.current = meta["title"]
                self.path = [self.current]
        else:
            self._ensure(self.start)
            self._ensure(self.goal)
        return self.observe()

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()

    def _ensure(self, title: str) -> None:
        if self.wiki is None:
            return
        if title in self.extracts:
            return
        extract, links = self.wiki.get(title)
        self.extracts[title] = extract
        self.link_cache[title] = links

    def _short_extract(self, title: str) -> str:
        if title in self.extracts:
            return self.extracts[title][:EXTRACT_CACHE_CHARS]
        if self.wiki is not None:
            self._ensure(title)
            return self.extracts.get(title, "")[:EXTRACT_CACHE_CHARS]
        return ""

    def _goal_reached(self, title: str) -> bool:
        return normalize_wiki_title(title) == normalize_wiki_title(self.goal)

    def observe(self) -> RaceState:
        if self.is_browser:
            assert self.browser is not None
            meta = self.browser.current_meta()
            if meta["title"]:
                self.current = meta["title"]
            visible = self.browser.observe_links()
            candidates = [
                Candidate(id=v.id, title=v.title, href=v.href, text=v.text)
                for v in visible
            ]
            current_extract = meta.get("extract", "")[:EXTRACT_CACHE_CHARS]
            goal_extract = self.extracts.get(self.goal, "")
            if not goal_extract:
                goal_extract = f"Reach the Wikipedia article titled {self.goal}."
            state = RaceState(
                goal=PageRef(title=self.goal, extract=goal_extract[:EXTRACT_CACHE_CHARS]),
                current=PageRef(title=self.current, extract=current_extract),
                history=list(self.path),
                step=self.step_count,
                max_steps=self.max_steps,
                candidates=candidates,
                source=self.source,
            )
            self._last_state = state
            return state

        # Fixture / MediaWiki API: full-page links
        assert self.wiki is not None
        self._ensure(self.current)
        raw_links = self.link_cache[self.current]
        links = raw_links[:MAX_CANDIDATES]
        candidates = []
        for i, title in enumerate(links, start=1):
            try:
                extract = self._short_extract(title)
            except Exception:
                extract = ""
            candidates.append(
                Candidate(
                    id=f"L{i:03d}",
                    title=title,
                    href="",
                    text=title,
                    extract=extract,
                )
            )
        state = RaceState(
            goal=PageRef(title=self.goal, extract=self._short_extract(self.goal)),
            current=PageRef(title=self.current, extract=self._short_extract(self.current)),
            history=list(self.path),
            step=self.step_count,
            max_steps=self.max_steps,
            candidates=candidates,
            source=self.source,
        )
        self._last_state = state
        return state

    def step(self, action_raw: Action | dict | str) -> StepResult:
        """Apply one action. Illegal click = fail. Scroll/translate consume a step."""
        # Legacy: bare link_id string
        if isinstance(action_raw, str):
            action_raw = {"action": "click", "link_id": action_raw}
        try:
            action = parse_action(action_raw)
        except Exception as exc:
            return StepResult(
                False, None, f"bad_action:{exc}", failed=True, action="invalid"
            )

        state = self._last_state or self.observe()

        if action.action == "click":
            return self._step_click(action, state)
        if action.action == "scroll":
            return self._step_scroll(action)
        if action.action == "translate":
            return self._step_translate(action)
        return StepResult(False, None, f"unknown_action:{action.action}", failed=True)

    def _bump_step(self) -> bool:
        """Increment step; return True if max_steps exhausted."""
        self.step_count += 1
        return self.step_count >= self.max_steps

    def _step_click(self, action: Action, state: RaceState) -> StepResult:
        link_id = action.link_id or ""
        cand = state.candidate_for(link_id)
        if cand is None:
            return StepResult(
                False, None, f"illegal_id:{link_id}", failed=True, action="click"
            )

        if self.is_browser:
            assert self.browser is not None
            hit = self.browser.click_link(link_id)
            if hit is None:
                return StepResult(
                    False, None, f"illegal_id:{link_id}", failed=True, action="click"
                )
            meta = self.browser.current_meta()
            title = meta["title"] or hit.title
        else:
            title = cand.title
            self.current = title

        self.current = title
        self.path.append(title)
        exhausted = self._bump_step()

        if self._goal_reached(title):
            return StepResult(True, title, "reached_goal", done=True, action="click")
        if exhausted:
            return StepResult(True, title, "max_steps", failed=True, action="click")
        return StepResult(True, title, "moved", action="click")

    def _step_scroll(self, action: Action) -> StepResult:
        direction = action.direction or "down"
        amount = action.amount or "page"
        if direction not in ("up", "down"):
            return StepResult(
                False, None, f"bad_direction:{direction}", failed=True, action="scroll"
            )
        if self.is_browser:
            assert self.browser is not None
            self.browser.scroll(direction=direction, amount=amount or "page")
        # Offline: scroll is a no-op on link set but still consumes a step
        exhausted = self._bump_step()
        if exhausted:
            return StepResult(
                True, self.current, "max_steps", failed=True, action="scroll"
            )
        return StepResult(True, self.current, f"scrolled_{direction}", action="scroll")

    def _step_translate(self, action: Action) -> StepResult:
        target = (action.target_lang or "").strip()
        if not target:
            return StepResult(
                False, None, "missing_target_lang", failed=True, action="translate"
            )
        if self.is_browser:
            assert self.browser is not None
            self.browser.translate(target)
        exhausted = self._bump_step()
        if exhausted:
            return StepResult(
                True, self.current, "max_steps", failed=True, action="translate"
            )
        return StepResult(
            True, self.current, f"translated:{target}", action="translate"
        )
