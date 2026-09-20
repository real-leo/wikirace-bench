import pytest

from wikirace import eval as evaluator
from wikirace.brains import JevBrain, LayaBrain
from wikirace.browser import VisibleLink, WikiBrowser
from wikirace.env import RaceEnv, PAGE_SHORTLIST_K
from wikirace.state import Action


class ArticleBrowser:
    def __init__(self, count):
        self.title = "Start"
        self.links = [VisibleLink(f"L{i:03}", "Goal" if i == count else f"Bridge {i}",
                                 f"https://en.wikipedia.org/wiki/Article_{i}", f"Link {i}",
                                 abs_y=i * 900) for i in range(1, count + 1)]
        self.collections = 0
        self.closed = False

    def open_article(self, title):
        self.title = title

    def current_meta(self):
        return {"title": self.title, "extract": "Description", "url": "/wiki/" + self.title}

    def observe_links(self, scope):
        assert scope == "page"
        self.collections += 1
        return self.links

    def link_in_viewport(self, link_id):
        pytest.fail("Full-page click must not trigger viewport recovery")

    def click_link(self, link_id):
        link = next(c for c in self.links if c.id == link_id)
        self.title = link.title
        return link

    def close(self):
        self.closed = True


class StubPageJev(JevBrain):
    def __init__(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
        super().__init__()
        self.requests = []

    def _post(self, payload, **kwargs):
        self.requests.append(payload)
        if "next" in payload["questions"]:
            choices = payload["questions"]["next"]["criteria"]
            lid = next(k for k, c in choices.items() if c["title"] == "Goal")
            return {"answers": {"next": {"choice": lid}}}
        candidates = payload["state"]["candidates"]
        return {"answers": {f"score_{k}": {"score": 4 if c["title"] == "Goal" else 1}
                            for k, c in candidates.items()}}


def make_page_env(count):
    browser = ArticleBrowser(count)
    env = RaceEnv(start="Start", goal="Goal", browser=browser, observation_mode="page",
                  extracts={"Goal": "Specific goal description"})
    return env, browser


@pytest.mark.parametrize("count", [2, 255, 601])
def test_page_pipeline_covers_all_links_and_clicks_offscreen_without_scroll(monkeypatch, count):
    env, browser = make_page_env(count)
    brain = StubPageJev(monkeypatch)
    monkeypatch.setattr(evaluator, "make_env", lambda *a, **kw: env)
    row = evaluator.run_episode({"start": "Start", "goal": "Goal", "observation_mode": "page"}, brain)
    assert row["status"] == "success", row["reason"]
    assert (row["steps"], row["clicks"], row["scrolls"]) == (1, 1, 0)
    assert row["finalist_picks"] == 0 and not row["finalist_mode_fired"]
    assert browser.collections == 1 and browser.closed
    assert row["trace"][0]["n_page_links"] == count
    criteria = brain.requests[-1]["questions"]["next"]["criteria"]
    assert "SCROLL_DOWN" not in criteria
    assert criteria[f"L{count:03}"]["url"].endswith(f"Article_{count}")
    if count <= 255:
        assert len(brain.requests) == 1 and len(criteria) == count
    else:
        score_ids = [k for r in brain.requests[:-1] for k in r["state"]["candidates"]]
        assert len(score_ids) == len(set(score_ids)) == count
        assert len(criteria) == PAGE_SHORTLIST_K
        assert all(len(r["questions"]) <= 64 for r in brain.requests[:-1])


def test_page_mode_rejects_scroll_and_click_outside_ranked_offered_set(monkeypatch):
    env, browser = make_page_env(300)
    brain = StubPageJev(monkeypatch)
    state = env.observe()
    scores, _ = brain.score_only(state)
    env.ingest_scores(scores)
    state = env.refresh_finalists()
    excluded = next(c for c in state.page_links if c.id not in state.offered_ids())
    assert env.step(Action(action="scroll", direction="down")).reason == "page_mode_click_only"
    assert env.step(Action(action="click", link_id=excluded.id)).reason.startswith("illegal_id")
    assert env.step_count == 0


def test_page_score_failure_is_not_silently_ranked_as_zero(monkeypatch):
    env, _ = make_page_env(300)
    brain = StubPageJev(monkeypatch)
    monkeypatch.setattr(brain, "_post", lambda payload: {"answers": {}})
    with pytest.raises(ValueError, match="missing_page_score"):
        brain.score_only(env.observe())


def test_later_score_batch_failure_preserves_completed_usage_and_never_clicks(monkeypatch):
    env, browser = make_page_env(601)
    brain = StubPageJev(monkeypatch)
    real_post = brain._post

    def fail_second_batch(payload):
        if brain.requests:
            raise RuntimeError("test_transport_failure")
        response = real_post(payload)
        response["usage"] = {"input_tokens": 123, "output_tokens": 64}
        return response

    monkeypatch.setattr(brain, "_post", fail_second_batch)
    monkeypatch.setattr(evaluator, "make_env", lambda *a, **kw: env)
    row = evaluator.run_episode({"start": "Start", "goal": "Goal", "observation_mode": "page"}, brain)
    assert row["status"] == "error" and "test_transport_failure" in row["reason"]
    assert (row["steps"], row["clicks"], row["scrolls"]) == (0, 0, 0)
    assert row["trace"][0]["brain_debug"]["partial_scoring"]
    assert row["trace"][0]["brain_debug"]["n_scored"] == 64
    assert row["api_usage"]["requests"] == 1
    assert row["api_usage"]["input_tokens"] == 123
    assert browser.closed


def test_unscored_large_page_cannot_be_truncated_to_first_links():
    env, _ = make_page_env(300)
    with pytest.raises(RuntimeError, match="incomplete_page_scores"):
        env.refresh_finalists()


def test_broken_page_extraction_is_not_an_empty_candidate_list(monkeypatch):
    browser = WikiBrowser()
    monkeypatch.setattr(browser, "_ensure", lambda: object())
    monkeypatch.setattr(browser, "scroll_metrics", lambda: {"y": 0})
    monkeypatch.setattr(browser, "_run_js", lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="invalid_article_links_snapshot"):
        browser.observe_links(scope="page")


def test_page_mode_accepts_laya_name(monkeypatch):
    """Page mode allowlist includes laya (same policy as Jev via page_policy)."""
    env, browser = make_page_env(2)

    class StubPageLaya(LayaBrain):
        def __init__(self):
            super().__init__(model="laya-test")
            self.requests = []

        def _post(self, payload, **kwargs):
            self.requests.append(payload)
            if "next" in payload["questions"]:
                choices = payload["questions"]["next"]["criteria"]
                lid = next(k for k, c in choices.items() if c["title"] == "Goal")
                return {"answers": {"next": {"choice": lid}}}
            candidates = payload["state"]["candidates"]
            return {"answers": {f"score_{k}": {"score": 4 if c["title"] == "Goal" else 1}
                                for k, c in candidates.items()}}

    brain = StubPageLaya()
    monkeypatch.setattr(evaluator, "make_env", lambda *a, **kw: env)
    row = evaluator.run_episode(
        {"start": "Start", "goal": "Goal", "observation_mode": "page", "source": "browser"},
        brain,
    )
    assert row["status"] == "success", row["reason"]
    assert row["brain"] == "laya"
    assert (row["steps"], row["clicks"], row["scrolls"]) == (1, 1, 0)
    assert browser.closed


def test_laya_brain_has_post_for_page_policy():
    """page_policy.score_page / choose_page call brain._post — Laya must expose it."""
    assert callable(getattr(LayaBrain, "_post", None))


def test_laya_post_short_circuits_single_choice():
    """Laya topk(2) crashes on 1-option Choice; _post must short-circuit."""
    brain = LayaBrain(model="laya-test")
    called = {"n": 0}

    def boom(state, questions):
        called["n"] += 1
        raise AssertionError("predict should not run for k=1 Choice")

    brain._predict = boom  # type: ignore[method-assign]
    out = brain._post({
        "model": "laya-test",
        "state": {"task": "t"},
        "questions": {
            "next": {
                "type": "choice",
                "instructions": {"question": "pick"},
                "criteria": {"L001": {"title": "Only"}},
            }
        },
    })
    assert called["n"] == 0
    assert out["answers"]["next"]["choice"] == "L001"
    assert out["answers"]["next"].get("short_circuit") is True

def test_laya_post_caps_large_choice_to_shortlist_k():
    """Direct page Choice can be ≤255; Laya head_max_len needs ≤PAGE_SHORTLIST_K."""
    brain = LayaBrain(model="laya-test")
    seen = {}

    def fake_predict(state, questions):
        crit = questions["next"]["criteria"]
        seen["n"] = len(crit)
        assert len(crit) <= PAGE_SHORTLIST_K
        assert any(c.get("title") == "Goal Article" for c in crit.values())
        return {
            "answers": {"next": {"choice": next(iter(crit)), "type": "choice"}},
            "model": "fake",
        }

    brain._predict = fake_predict  # type: ignore[method-assign]
    criteria = {
        f"L{i:03}": {"title": f"Link {i}", "bridge_score": i / 300} for i in range(200)
    }
    criteria["L999"] = {"title": "Goal Article", "bridge_score": 0.1}
    out = brain._post({
        "model": "laya-test",
        "state": {"goal": {"title": "Goal Article"}},
        "questions": {
            "next": {
                "type": "choice",
                "instructions": {"question": "pick"},
                "criteria": criteria,
            }
        },
    })
    assert seen["n"] == PAGE_SHORTLIST_K
    assert "next" in out["answers"]
