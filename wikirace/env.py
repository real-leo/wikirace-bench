from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from wikirace.browser import WikiBrowser, normalize_wiki_title
from wikirace.state import Action, ActionState, Candidate, PageRef, RaceState, parse_action
from wikirace.wiki import WikiSource

if TYPE_CHECKING:
    pass

MAX_CANDIDATES = 255
EXTRACT_CACHE_CHARS = 180
# Recovery scrolls when clicking an off-screen memory link (cap per click).
MAX_RECOVERY_SCROLLS = 20
# Consecutive alternating up/down scrolls with no click → fail (nice-to-have).
SCROLL_OSCILLATION_LIMIT = 12


@dataclass
class StepResult:
    ok: bool
    title: str | None
    reason: str = ""
    done: bool = False
    failed: bool = False
    action: str = ""
    # How many actions this step consumed (recovery scrolls + click can be >1)
    actions_consumed: int = 1
    recovery_scrolls: int = 0


@dataclass
class RaceEnv:
    """WikiRace environment — equal-cost actions.

    Primary mode: source=browser (DrissionPage, viewport-visible links).
    Offline: fixture / live MediaWiki API (full-page links, no real scroll).

    Every action (click, scroll, translate) counts equally in metrics (steps).
    Episodes do NOT fail on step count — termination is goal / illegal action /
    wall-clock timeout / optional scroll oscillation. max_steps is soft info
    only (0 = unlimited in observation).
    Clicking a remembered off-screen link auto-scrolls toward it; each recovery
    scroll also counts as one step, then the click costs one more.
    At page bottom SCROLL_DOWN is not offered; at top SCROLL_UP is not offered.
    """

    start: str
    goal: str
    max_steps: int = 0  # 0 = unlimited (soft info only; never a fail condition)
    source: str = "browser"
    wiki: WikiSource | None = None
    browser: WikiBrowser | None = None
    lang: str = "en"
    current: str = ""
    path: list[str] = field(default_factory=list)
    step_count: int = 0  # total actions (clicks + scrolls + translates + recovery)
    click_count: int = 0
    scroll_count: int = 0
    extracts: dict[str, str] = field(default_factory=dict)
    link_cache: dict[str, list[str]] = field(default_factory=dict)
    _last_state: RaceState | None = None
    # Previous viewport candidates (public Candidate list) for memory union
    _prev_viewport: list[Candidate] = field(default_factory=list)
    _curr_viewport: list[Candidate] = field(default_factory=list)
    # Model scroll directions since last click (for oscillation detection)
    _scroll_dirs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.reset()

    @property
    def is_browser(self) -> bool:
        return self.source in ("browser", "live_browser") and self.browser is not None

    def reset(self) -> RaceState:
        self.current = self.start
        self.path = [self.start]
        self.step_count = 0
        self.click_count = 0
        self.scroll_count = 0
        self._last_state = None
        self._prev_viewport = []
        self._curr_viewport = []
        self._scroll_dirs = []
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

    def _scroll_flags(self) -> tuple[bool, bool]:
        if not self.is_browser:
            return False, False
        assert self.browser is not None
        m = self.browser.scroll_metrics()
        can_down = not bool(m.get("at_bottom"))
        can_up = not bool(m.get("at_top"))
        return can_down, can_up

    def _build_memory(self, viewport: list[Candidate]) -> list[Candidate]:
        """Union of previous screen + current screen; prefer keeping both fully."""
        by_id: dict[str, Candidate] = {}
        for c in self._prev_viewport:
            # Mark previous-screen links
            by_id[c.id] = c.model_copy(
                update={"position": c.position if c.position.startswith("scrollY") else "prev_viewport"}
            )
        for c in viewport:
            by_id[c.id] = c  # current wins on overlap
        # Cap if enormous (Choice limit 255 including scroll options)
        items = list(by_id.values())
        if len(items) > MAX_CANDIDATES - 2:
            # Prefer all current, then fill from prev
            cur_ids = {c.id for c in viewport}
            cur = [c for c in items if c.id in cur_ids]
            prev = [c for c in items if c.id not in cur_ids]
            items = (cur + prev)[: MAX_CANDIDATES - 2]
        return items

    def observe(self) -> RaceState:
        can_down, can_up = self._scroll_flags()
        if self.max_steps <= 0:
            remaining: int | None = None
        else:
            remaining = max(0, self.max_steps - self.step_count)

        if self.is_browser:
            assert self.browser is not None
            meta = self.browser.current_meta()
            if meta["title"]:
                self.current = meta["title"]
            visible = self.browser.observe_links()
            metrics = self.browser.scroll_metrics()
            scroll_y = float(metrics.get("y") or 0)
            viewport = [
                Candidate(
                    id=v.id,
                    title=v.title,
                    context=v.context or v.text,
                    text=v.text,
                    position=f"current_viewport|scrollY={int(scroll_y)}",
                )
                for v in visible
            ]
            # On first observe after navigation, prev is empty; after scroll,
            # caller should have rotated prev←curr before observe. If not yet
            # rotated (first call), prev stays [].
            memory = self._build_memory(viewport)
            self._curr_viewport = viewport

            current_extract = meta.get("extract", "")[:EXTRACT_CACHE_CHARS]
            goal_extract = self.extracts.get(self.goal, "")
            if not goal_extract:
                goal_extract = f"Reach the Wikipedia article titled {self.goal}."

            action_state = ActionState(
                path=list(self.path),
                step=self.step_count,
                max_steps=self.max_steps,
                remaining_steps=remaining,
                can_scroll_down=can_down,
                can_scroll_up=can_up,
            )
            state = RaceState(
                goal=PageRef(
                    title=self.goal,
                    description=goal_extract[:EXTRACT_CACHE_CHARS],
                ),
                current=PageRef(title=self.current, description=current_extract),
                viewport=viewport,
                memory=memory,
                action=action_state,
                history=list(self.path),
                step=self.step_count,
                max_steps=self.max_steps,
                candidates=memory,  # offered set = memory union
                source=self.source,
            )
            self._last_state = state
            return state

        # Fixture / MediaWiki API: full-page links (no real viewport/scroll)
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
                    context=extract or title,
                    text=title,
                    extract=extract,
                    position="current_viewport",
                )
            )
        action_state = ActionState(
            path=list(self.path),
            step=self.step_count,
            max_steps=self.max_steps,
            remaining_steps=remaining,
            can_scroll_down=False,
            can_scroll_up=False,
        )
        state = RaceState(
            goal=PageRef(title=self.goal, description=self._short_extract(self.goal)),
            current=PageRef(
                title=self.current, description=self._short_extract(self.current)
            ),
            viewport=candidates,
            memory=candidates,
            action=action_state,
            history=list(self.path),
            step=self.step_count,
            max_steps=self.max_steps,
            candidates=candidates,
            source=self.source,
        )
        self._last_state = state
        return state

    def _rotate_viewport_memory(self) -> None:
        """After a scroll, current becomes previous for the next observe."""
        self._prev_viewport = list(self._curr_viewport)

    def _bump(self, n: int = 1) -> None:
        """Consume n actions for equal-cost metrics (never a fail condition)."""
        self.step_count += n

    def step(self, action_raw: Action | dict | str) -> StepResult:
        """Apply one brain action. Scroll and click each count ≥1 in step metrics."""
        if isinstance(action_raw, str):
            action_raw = parse_action(action_raw)
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
            return self._step_scroll(action, state)
        if action.action == "translate":
            return self._step_translate(action)
        return StepResult(False, None, f"unknown_action:{action.action}", failed=True)

    def _step_click(self, action: Action, state: RaceState) -> StepResult:
        link_id = action.link_id or ""
        cand = state.candidate_for(link_id)
        if cand is None:
            return StepResult(
                False, None, f"illegal_id:{link_id}", failed=True, action="click"
            )

        recovery = 0
        if self.is_browser:
            assert self.browser is not None
            # If remembered but not currently visible, auto-scroll toward it
            # (each recovery scroll counts as one step), then click.
            if not self.browser.link_in_viewport(link_id):
                while (
                    not self.browser.link_in_viewport(link_id)
                    and recovery < MAX_RECOVERY_SCROLLS
                ):
                    metrics = self.browser.scroll_toward_link(link_id)
                    recovery += 1
                    self.scroll_count += 1
                    if not metrics.get("changed") and not metrics.get("in_viewport"):
                        # Stuck; stop recovery
                        break
                    if metrics.get("in_viewport"):
                        break
                self.step_count += recovery
                if not self.browser.link_in_viewport(link_id):
                    # Fall through: try click via registry href / navigate
                    pass

            hit = self.browser.click_link(link_id)
            if hit is None:
                return StepResult(
                    False,
                    None,
                    f"illegal_id:{link_id}",
                    failed=True,
                    action="click",
                    actions_consumed=recovery,
                    recovery_scrolls=recovery,
                )
            meta = self.browser.current_meta()
            title = meta["title"] or hit.title
            # New page: clear viewport memory
            self._prev_viewport = []
            self._curr_viewport = []
        else:
            title = cand.title
            self.current = title

        self.current = title
        self.path.append(title)
        self.click_count += 1
        self._scroll_dirs = []  # click breaks scroll oscillation streak
        self._bump(1)

        if self._goal_reached(title):
            return StepResult(
                True,
                title,
                "reached_goal",
                done=True,
                action="click",
                actions_consumed=recovery + 1,
                recovery_scrolls=recovery,
            )
        return StepResult(
            True,
            title,
            "moved",
            action="click",
            actions_consumed=recovery + 1,
            recovery_scrolls=recovery,
        )

    def _step_scroll(self, action: Action, state: RaceState) -> StepResult:
        direction = action.direction or "down"
        amount = action.amount or "page"
        if direction not in ("up", "down"):
            return StepResult(
                False, None, f"bad_direction:{direction}", failed=True, action="scroll"
            )

        # Physical: refuse scroll past edges (should not be offered, but guard)
        if direction == "down" and not state.action.can_scroll_down:
            return StepResult(
                False,
                self.current,
                "scroll_down_unavailable",
                failed=True,
                action="scroll",
            )
        if direction == "up" and not state.action.can_scroll_up:
            return StepResult(
                False,
                self.current,
                "scroll_up_unavailable",
                failed=True,
                action="scroll",
            )

        # Rotate memory: current viewport becomes previous before scrolling
        self._rotate_viewport_memory()

        if self.is_browser:
            assert self.browser is not None
            self.browser.scroll(direction=direction, amount=amount or "page")

        self.scroll_count += 1
        self._bump(1)
        self._scroll_dirs.append(direction)
        if self._is_scroll_oscillating():
            return StepResult(
                True,
                self.current,
                "scroll_oscillation",
                failed=True,
                action="scroll",
            )
        return StepResult(
            True, self.current, f"scrolled_{direction}", action="scroll"
        )

    def _is_scroll_oscillating(self) -> bool:
        """Fail if many consecutive model scrolls strictly alternate up/down."""
        dirs = self._scroll_dirs
        if len(dirs) < SCROLL_OSCILLATION_LIMIT:
            return False
        window = dirs[-SCROLL_OSCILLATION_LIMIT:]
        return all(window[i] != window[i + 1] for i in range(len(window) - 1))

    def _step_translate(self, action: Action) -> StepResult:
        target = (action.target_lang or "").strip()
        if not target:
            return StepResult(
                False, None, "missing_target_lang", failed=True, action="translate"
            )
        if self.is_browser:
            assert self.browser is not None
            self.browser.translate(target)
        self._prev_viewport = []
        self._curr_viewport = []
        self._scroll_dirs = []
        self._bump(1)
        return StepResult(
            True, self.current, f"translated:{target}", action="translate"
        )
