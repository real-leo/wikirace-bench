from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from wikirace.browser import WikiBrowser, normalize_wiki_title
from wikirace.state import (
    Action,
    ActionState,
    Candidate,
    LinkScore,
    PageRef,
    RaceState,
    parse_action,
)
from wikirace.wiki import EXTRACT_CHARS, WikiSource, fetch_intro_extract

if TYPE_CHECKING:
    pass

MAX_CANDIDATES = 255
EXTRACT_CACHE_CHARS = EXTRACT_CHARS  # same length cap as goal / page intro extracts
# Recovery scrolls when clicking an off-screen memory link (cap per click).
MAX_RECOVERY_SCROLLS = 20
# Top-K highest-scored links forced at page bottom (finalist mode).
FINALIST_K = 5


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
    """WikiRace environment — equal-cost scroll-down / click.

    Primary mode: source=browser (DrissionPage, viewport-visible links).
    Offline: fixture / live MediaWiki API (full-page links, no real scroll).

    Model actions: scroll DOWN + click link_id (no SCROLL_UP).
    At page bottom (can_scroll_down=False) observation enters finalist_mode:
    the brain must choose among the top-K highest-scored links seen on this page.

    Every action (click, scroll, translate) counts equally in metrics (steps).
    Episodes do NOT fail on step count — termination is goal / illegal action /
    wall-clock timeout / finalist_empty. max_steps is soft info only
    (0 = unlimited in observation).
    Clicking a remembered off-screen link auto-scrolls toward it; each recovery
    scroll also counts as one step, then the click costs one more.
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
    # Model scrolls on the current page (resets on navigation)
    page_scroll_count: int = 0
    # Best bridge score per link title on the current page
    _page_scores: dict[str, LinkScore] = field(default_factory=dict)
    # Episode-wide best scores (keep highest; for metrics)
    _episode_scores: dict[str, LinkScore] = field(default_factory=dict)
    finalist_k: int = FINALIST_K
    last_chosen_score: float | None = None
    finalist_picks: int = 0
    # Titles blocked after revisits (handles redirects that bypass path filter).
    # Keyed by normalized title only — never by link id (ids like L001 reuse across pages).
    _blocked_titles: set[str] = field(default_factory=set)
    # Scores of links actually clicked this episode (for avg_clicked_score).
    _clicked_scores: list[float] = field(default_factory=list)

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
        self.page_scroll_count = 0
        self._last_state = None
        self._prev_viewport = []
        self._curr_viewport = []
        self._page_scores = {}
        self._episode_scores = {}
        self.last_chosen_score = None
        self.finalist_picks = 0
        self._blocked_titles = set()
        self._clicked_scores = []
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
        self._ensure_goal_extract()
        return self.observe()

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()


    def _ensure_goal_extract(self) -> str:
        """Load goal intro extract (same MediaWiki source/length as page extracts)."""
        cached = self.extracts.get(self.goal, "")
        if cached and not cached.startswith("Reach the Wikipedia article titled"):
            return cached[:EXTRACT_CACHE_CHARS]
        if self.wiki is not None:
            try:
                self._ensure(self.goal)
                got = self.extracts.get(self.goal, "")
                if got:
                    return got[:EXTRACT_CACHE_CHARS]
            except Exception:
                pass
        got = fetch_intro_extract(self.goal, lang=self.lang, max_chars=EXTRACT_CACHE_CHARS)
        if got:
            self.extracts[self.goal] = got
            return got
        fallback = f"Reach the Wikipedia article titled {self.goal}."
        self.extracts[self.goal] = fallback
        return fallback

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

    def _score_key(self, title: str) -> str:
        return normalize_wiki_title(title).lower()

    def _is_visited(self, title: str) -> bool:
        """True if title is already on the path (normalized). Goal is never 'visited' for filtering."""
        if self._goal_reached(title):
            return False
        key = self._score_key(title)
        if key in self._blocked_titles:
            return True
        return any(self._score_key(p) == key for p in self.path)

    def _filter_visited(self, cands: list[Candidate]) -> list[Candidate]:
        """Drop already-visited / blocked pages from offered clicks (keep goal).

        Blocking is by normalized title only — never by link id.
        """
        out: list[Candidate] = []
        for c in cands:
            if self._is_visited(c.title):
                continue
            out.append(c)
        return out

    def ingest_scores(self, scores: list[dict] | list[LinkScore]) -> None:
        """Merge scores into page + episode books, keeping the highest per title."""
        for raw in scores:
            if isinstance(raw, LinkScore):
                entry = raw
            else:
                entry = LinkScore(
                    id=str(raw.get("id") or ""),
                    title=str(raw.get("title") or ""),
                    context=str(raw.get("context") or "")[:280],
                    score=float(raw.get("score") or 0.0),
                    href_key=str(raw.get("href_key") or ""),
                )
            if not entry.title:
                continue
            key = self._score_key(entry.title)
            if not entry.href_key:
                entry = entry.model_copy(update={"href_key": key})
            for store in (self._page_scores, self._episode_scores):
                prev = store.get(key)
                if prev is None or entry.score >= prev.score:
                    store[key] = entry

    def top_k_finalists(self, k: int | None = None) -> list[LinkScore]:
        """Highest-scored *selectable* links seen on the current page.

        Visited/blocked titles are excluded *before* taking top-K so a valid
        #6 is never dropped because the top-5 were all already visited.
        Finalists stay scoped to the current page score book (no episode teleport).
        """
        k = self.finalist_k if k is None else k
        eligible = [
            s for s in self._page_scores.values() if not self._is_visited(s.title)
        ]
        ranked = sorted(eligible, key=lambda s: s.score, reverse=True)
        return ranked[:k]


    def refresh_finalists(self) -> RaceState:
        """Rebuild offered candidates / finalists from the updated page score book.

        Call after ``ingest_scores`` so Choice sees newly scored viewport links
        (including a last-viewport goal) before picking. Does not re-observe the
        browser — only re-ranks from ``_page_scores`` + current memory/viewport.
        """
        state = self._last_state
        if state is None:
            return self.observe()

        viewport = self._annotate_scores(list(self._curr_viewport or state.viewport))
        if self._curr_viewport or self._prev_viewport:
            memory = self._annotate_scores(self._build_memory(viewport))
        else:
            memory = self._annotate_scores(list(state.memory))
        if self._curr_viewport:
            self._curr_viewport = viewport

        finalist_mode = bool(state.action.finalist_mode)
        if finalist_mode:
            finalists = self._finalist_candidates(memory)
            offered = finalists
        else:
            finalists = []
            offered = self._filter_visited(memory)

        new_state = state.model_copy(
            update={
                "viewport": viewport,
                "memory": memory,
                "candidates": offered,
                "finalists": finalists,
                "page_scores": list(self._page_scores.values()),
            }
        )
        self._last_state = new_state
        return new_state

    def _annotate_scores(self, cands: list[Candidate]) -> list[Candidate]:
        out: list[Candidate] = []
        for c in cands:
            key = self._score_key(c.title)
            sc = self._page_scores.get(key) or self._episode_scores.get(key)
            if sc is not None:
                out.append(c.model_copy(update={"score": sc.score}))
            else:
                out.append(c)
        return out

    def _finalist_candidates(self, memory: list[Candidate]) -> list[Candidate]:
        """Map top-K page scores onto Candidate objects (prefer live memory ids)."""
        by_title: dict[str, Candidate] = {}
        for c in memory:
            by_title[self._score_key(c.title)] = c
        for c in list(self._curr_viewport) + list(self._prev_viewport):
            by_title.setdefault(self._score_key(c.title), c)

        finalists: list[Candidate] = []
        for sc in self.top_k_finalists():
            key = self._score_key(sc.title)
            live = by_title.get(key)
            if live is not None:
                finalists.append(
                    live.model_copy(
                        update={
                            "score": sc.score,
                            "context": sc.context or live.context,
                        }
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

    def _clear_page_books(self) -> None:
        self._page_scores = {}
        self.page_scroll_count = 0
        self._prev_viewport = []
        self._curr_viewport = []

    def score_metrics(self) -> dict:
        """Score book metrics.

        ``avg_score`` is the mean over *all* scored titles this episode (global
        score book), NOT a path-quality metric. Use ``avg_clicked_score`` for
        mean bridge score of links actually clicked.
        """
        vals = [s.score for s in self._episode_scores.values()]
        clicked = list(self._clicked_scores)
        return {
            "n_scored_links": len(vals),
            "max_score": max(vals) if vals else None,
            # Mean over all scored titles on the episode (not path-only).
            "avg_score": round(sum(vals) / len(vals), 3) if vals else None,
            "avg_score_scope": "all_scored_titles",
            "avg_clicked_score": (
                round(sum(clicked) / len(clicked), 3) if clicked else None
            ),
            "chosen_score": self.last_chosen_score,
            "finalist_picks": self.finalist_picks,
        }

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
        can_down, _can_up = self._scroll_flags()
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
            viewport = self._annotate_scores(viewport)
            memory = self._annotate_scores(self._build_memory(viewport))
            self._curr_viewport = viewport

            current_extract = meta.get("extract", "")[:EXTRACT_CACHE_CHARS]
            if current_extract:
                self.extracts[self.current] = current_extract
            goal_extract = self._ensure_goal_extract()

            # Finalist mode: at page bottom the model must pick among top-K scored links.
            # Visited/blocked filtered BEFORE top-K inside top_k_finalists / _finalist_candidates.
            finalist_mode = not can_down
            if finalist_mode:
                finalists = self._finalist_candidates(memory)
                offered = finalists
            else:
                finalists = []
                offered = self._filter_visited(memory)

            action_state = ActionState(
                path=list(self.path),
                step=self.step_count,
                max_steps=self.max_steps,
                remaining_steps=remaining,
                can_scroll_down=can_down,
                can_scroll_up=False,  # never offered to the model
                page_scroll_count=self.page_scroll_count,
                finalist_mode=finalist_mode,
                finalist_k=self.finalist_k,
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
                candidates=offered,
                finalists=finalists,
                page_scores=list(self._page_scores.values()),
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
        candidates = self._annotate_scores(candidates)
        candidates = self._filter_visited(candidates)
        action_state = ActionState(
            path=list(self.path),
            step=self.step_count,
            max_steps=self.max_steps,
            remaining_steps=remaining,
            can_scroll_down=False,
            can_scroll_up=False,
            page_scroll_count=0,
            finalist_mode=False,
            finalist_k=self.finalist_k,
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
            finalists=[],
            page_scores=list(self._page_scores.values()),
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
        # Strict: only ids in the offered set for this step (finalists or viewport∪memory).
        offered = state.offered_ids()
        if link_id not in offered:
            return StepResult(
                False, None, f"illegal_id:{link_id}", failed=True, action="click"
            )
        cand = state.candidate_for(link_id)
        if cand is None:
            return StepResult(
                False, None, f"illegal_id:{link_id}", failed=True, action="click"
            )

        key = self._score_key(cand.title)
        sc = self._page_scores.get(key) or self._episode_scores.get(key)
        self.last_chosen_score = sc.score if sc is not None else cand.score
        if self.last_chosen_score is not None:
            self._clicked_scores.append(float(self.last_chosen_score))
        if state.action.finalist_mode:
            self.finalist_picks += 1

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
        else:
            title = cand.title
            self.current = title

        # Block path ping-pong / redirect revisits: if we landed on a page
        # already on the path, blacklist this link id and landed title.
        landed_key = self._score_key(title)
        already = any(self._score_key(p) == landed_key for p in self.path)
        if already and not self._goal_reached(title):
            self._blocked_titles.add(landed_key)
            self._blocked_titles.add(self._score_key(cand.title))
            # Stay on the revisited page but do not grow the path again.
            self.current = title
            self.click_count += 1
            self._clear_page_books()
            self._bump(1)
            return StepResult(
                True,
                title,
                "revisit_blocked",
                action="click",
                actions_consumed=recovery + 1,
                recovery_scrolls=recovery,
            )

        self._blocked_titles.add(landed_key)
        self._blocked_titles.add(self._score_key(cand.title))
        self.current = title
        self.path.append(title)
        self.click_count += 1
        self._clear_page_books()
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
        # Model-facing scroll is down-only (recovery uses browser.scroll directly).
        if direction != "down":
            return StepResult(
                False,
                self.current,
                "scroll_up_removed",
                failed=True,
                action="scroll",
            )

        if not state.action.can_scroll_down:
            return StepResult(
                False,
                self.current,
                "scroll_down_unavailable",
                failed=True,
                action="scroll",
            )

        # Rotate memory: current viewport becomes previous before scrolling
        self._rotate_viewport_memory()

        if self.is_browser:
            assert self.browser is not None
            self.browser.scroll(direction="down", amount=amount or "page")

        self.scroll_count += 1
        self.page_scroll_count += 1
        self._bump(1)
        return StepResult(True, self.current, "scrolled_down", action="scroll")

    def _step_translate(self, action: Action) -> StepResult:
        target = (action.target_lang or "").strip()
        if not target:
            return StepResult(
                False, None, "missing_target_lang", failed=True, action="translate"
            )
        if self.is_browser:
            assert self.browser is not None
            self.browser.translate(target)
        self._clear_page_books()
        self._bump(1)
        return StepResult(
            True, self.current, f"translated:{target}", action="translate"
        )
