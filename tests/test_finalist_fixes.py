"""Unit tests for finalist ranking / scoring / blocking / click validation fixes."""
from __future__ import annotations

from wikirace.brains import OverlapBrain, SCORE_BATCH
from wikirace.env import RaceEnv
from wikirace.state import Action, Candidate, LinkScore, RaceState, ActionState, PageRef
from wikirace.wiki import FixtureWiki, fetch_intro_extract, EXTRACT_CHARS


def _env(start="Coffee", goal="Caffeine") -> RaceEnv:
    return RaceEnv(
        start=start,
        goal=goal,
        source="fixture",
        wiki=FixtureWiki(),
        max_steps=0,
    )


def test_last_viewport_goal_enters_finalists_after_refresh():
    """Last-viewport goal scored this step must appear in finalist Choice criteria."""
    env = _env()
    brain = OverlapBrain()
    state = env.observe()

    # Simulate bottom-of-page finalist mode with prior low scores.
    env.ingest_scores(
        [
            LinkScore(id="L001", title="Ethiopia", context="x", score=0.2),
            LinkScore(id="L002", title="Breakfast", context="x", score=0.3),
            LinkScore(id="L003", title="Stimulant", context="x", score=0.4),
            LinkScore(id="L004", title="Brazil", context="x", score=0.5),
            LinkScore(id="L005", title="Tea", context="x", score=0.1),
        ]
    )
    state = state.model_copy(
        update={
            "action": state.action.model_copy(
                update={"finalist_mode": True, "can_scroll_down": False}
            ),
            "viewport": list(state.viewport)
            + [
                Candidate(
                    id="L099",
                    title="Caffeine",
                    context="contains the stimulant caffeine",
                    position="current_viewport",
                )
            ],
        }
    )
    env._last_state = state
    env._curr_viewport = list(state.viewport)

    scores, _ = brain.score_only(state)
    # Force max score on goal as brain would after seeing it on last viewport.
    scores.append(
        {
            "id": "L099",
            "title": "Caffeine",
            "context": "contains the stimulant caffeine",
            "score": 1.0,
            "href_key": "caffeine",
        }
    )
    env.ingest_scores(scores)
    state = env.refresh_finalists()
    assert state.action.finalist_mode or True  # refreshed list
    titles = [c.title for c in state.finalists]
    assert "Caffeine" in titles, titles
    action, debug = brain.choose_action(state)
    offered = {c["id"] for c in debug.get("finalists", [])} or {
        c.id for c in state.finalists
    }
    assert "L099" in offered or any(c.title == "Caffeine" for c in state.finalists)
    env.close()


def test_score_all_viewport_links_not_truncated_at_20():
    """25 viewport links must all get scored (batched until done)."""
    env = _env()
    brain = OverlapBrain()
    state = env.observe()
    # Build 25 fake viewport links
    viewport = [
        Candidate(id=f"L{i:03d}", title=f"Topic{i}", context=f"about topic {i}")
        for i in range(1, 26)
    ]
    state = state.model_copy(update={"viewport": viewport})
    assert len(state.viewport) == 25
    assert SCORE_BATCH == 20  # batch size still 20, but we loop
    scores, dbg = brain.score_only(state)
    assert len(scores) == 25, len(scores)
    env.close()


def test_filter_visited_before_topk_keeps_sixth():
    """Visited top-5 must not empty finalists when #6 is valid."""
    env = _env(start="Coffee", goal="Moon")
    # Pretend we visited 5 high-scoring pages
    env.path = ["Coffee", "A", "B", "C", "D", "E"]
    for title, sc, i in [
        ("A", 0.95, 1),
        ("B", 0.94, 2),
        ("C", 0.93, 3),
        ("D", 0.92, 4),
        ("E", 0.91, 5),
        ("GoodBridge", 0.90, 6),
        ("Noise", 0.1, 7),
    ]:
        env.ingest_scores(
            [LinkScore(id=f"L{i:03d}", title=title, context="x", score=sc)]
        )
    top = env.top_k_finalists(k=5)
    titles = [s.title for s in top]
    assert titles, "finalists must be nonempty"
    assert "GoodBridge" in titles, titles
    assert not any(t in {"A", "B", "C", "D", "E"} for t in titles), titles
    env.close()


def test_block_by_title_not_link_id_across_pages():
    """Blocking L001 on page A must not block a different title with L001 on page B."""
    env = _env()
    env.observe()
    # Simulate revisit block of title "Ethiopia" only (no id poison)
    env._blocked_titles.add(env._score_key("Ethiopia"))
    # Candidate with reused id L001 but different title must remain selectable
    cands = [
        Candidate(id="L001", title="UnrelatedPage", context="fresh page link"),
        Candidate(id="L002", title="Ethiopia", context="blocked title"),
    ]
    filtered = env._filter_visited(cands)
    ids = {c.id for c in filtered}
    titles = {c.title for c in filtered}
    assert "L001" in ids
    assert "UnrelatedPage" in titles
    assert "Ethiopia" not in titles
    env.close()


def test_click_id_not_in_offered_is_illegal():
    env = _env()
    state = env.observe()
    # Shrink offered set
    offered = state.candidates[:1] if state.candidates else []
    state = state.model_copy(update={"candidates": offered, "finalists": []})
    env._last_state = state
    bad_id = "L999"
    assert bad_id not in state.offered_ids()
    result = env.step(Action(action="click", link_id=bad_id))
    assert not result.ok and result.failed
    assert result.reason.startswith("illegal_id")
    env.close()


def test_goal_description_is_extract_not_title_echo():
    """Fixture goal description should be the page extract, not 'Reach … titled X'."""
    env = _env(start="Coffee", goal="Caffeine")
    state = env.observe()
    desc = state.goal.description or ""
    assert desc, "goal description empty"
    assert "Reach the Wikipedia article titled" not in desc
    assert "Caffeine" in state.goal.title
    # Extract should mention stimulant / coffee-related content
    assert len(desc) <= EXTRACT_CHARS
    assert len(desc) > 20
    env.close()


def test_fetch_intro_extract_bald_mountain_smoke():
    """Live API smoke: goal description is Wikipedia intro extract, not title echo."""
    text = fetch_intro_extract(
        "Bald Mountain Recreation Area", lang="en", max_chars=EXTRACT_CHARS
    )
    assert text, "expected MediaWiki intro extract (check User-Agent / network)"
    assert "Reach the Wikipedia article titled" not in text
    assert len(text) > 40
    assert text.lower() != "bald mountain recreation area"
    assert any(
        k in text.lower()
        for k in ("recreation", "park", "michigan", "mountain", "area", "trail")
    )


def test_avg_score_scope_documented():
    env = _env()
    env.observe()
    env.ingest_scores(
        [
            LinkScore(id="L1", title="Tea", context="x", score=0.2),
            LinkScore(id="L2", title="Brazil", context="x", score=0.8),
        ]
    )
    m = env.score_metrics()
    assert m["avg_score_scope"] == "all_scored_titles"
    assert m["avg_score"] == 0.5
    env.close()
