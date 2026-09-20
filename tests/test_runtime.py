"""Exercise real score/refresh/choice orchestration with deterministic transports."""
from __future__ import annotations

import pytest
import httpx

from wikirace import eval as evaluator
from wikirace.brains import Brain, JevBrain
from wikirace.browser import PageLoadError, VisibleLink, WikiBrowser, find_browser_path
from wikirace.env import RaceEnv
from wikirace.state import Action, Candidate, PageRef, RaceState
from wikirace.wiki import FixtureWiki


class StubJev(JevBrain):
    def __init__(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
        super().__init__()
        self.requests = []

    def _post(self, payload, **kwargs):
        self.requests.append(payload)
        if all(q["type"] == "score" for q in payload["questions"].values()):
            goal = payload["state"]["goal"]["title"]
            return {
                "model": "test-jev",
                "answers": {
                    f"score_{lid}": {"score": 4 if c["title"] == goal else 1}
                    for lid, c in payload["state"]["candidates"].items()
                },
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        criteria = payload["questions"]["next"]["criteria"]
        choice = "SCROLL_DOWN" if "SCROLL_DOWN" in criteria else max(
            criteria, key=lambda lid: criteria[lid].get("score") or 0
        )
        return {
            "model": "test-jev",
            "answers": {"next": {"choice": choice}},
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }


class TwoScreenBrowser:
    def __init__(self):
        self.title = "Start"
        self.bottom = False
        self.closed = False
        self.screens = [
            [VisibleLink("L001", "Old bridge", "/wiki/Old_bridge", "Old bridge")],
            [VisibleLink("L002", "Goal", "/wiki/Goal", "Goal")],
        ]

    def open_article(self, title):
        self.title = title

    def current_meta(self):
        return {"title": self.title, "extract": "Page description", "url": "/wiki/" + self.title}

    def observe_links(self):
        return self.screens[int(self.bottom)]

    def scroll_metrics(self):
        return {"at_bottom": self.bottom, "at_top": not self.bottom, "y": int(self.bottom) * 900}

    def scroll(self, **kwargs):
        self.bottom = True
        return {"changed": True}

    def link_in_viewport(self, link_id):
        return any(c.id == link_id for c in self.observe_links())

    def click_link(self, link_id):
        target = next(c for c in self.observe_links() if c.id == link_id)
        self.title = target.title
        return target

    def close(self):
        self.closed = True


def fixture_env():
    return RaceEnv(start="Coffee", goal="Caffeine", source="fixture", wiki=FixtureWiki())


def test_jev_scores_link_21_and_later(monkeypatch):
    brain = StubJev(monkeypatch)
    state = RaceState(
        goal=PageRef(title="Goal"), current=PageRef(title="Start"),
        viewport=[Candidate(id=f"L{i:03}", title="Goal" if i == 25 else str(i)) for i in range(1, 26)],
    )
    scores, debug = brain.score_only(state)
    assert len(scores) == 25
    assert [len(r["questions"]) for r in brain.requests] == [20, 5]
    assert next(s for s in scores if s["title"] == "Goal")["score"] == 1.0
    assert debug["n_scored"] == 25


def test_last_screen_goal_is_in_actual_choice_request(monkeypatch):
    browser = TwoScreenBrowser()
    env = RaceEnv(start="Start", goal="Goal", browser=browser, extracts={"Goal": "Goal description"})
    monkeypatch.setattr(evaluator, "make_env", lambda *args, **kwargs: env)
    brain = StubJev(monkeypatch)
    row = evaluator.run_episode({"start": "Start", "goal": "Goal"}, brain)
    assert row["status"] == "success", row["reason"]
    assert row["path"] == ["Start", "Goal"]
    assert (row["steps"], row["clicks"], row["scrolls"]) == (2, 1, 1)
    assert row["finalist_picks"] == 1
    assert "L002" in brain.requests[-1]["questions"]["next"]["criteria"]
    assert row["trace"][-1]["observation"]["finalists"][0]["title"] == "Goal"
    assert browser.closed


def test_known_memory_id_outside_offered_is_rejected():
    env = fixture_env()
    state = env.observe()
    bad_id = state.candidates[1].id
    env._last_state = state.model_copy(update={"candidates": state.candidates[:1]})
    assert any(c.id == bad_id for c in env._last_state.memory)
    result = env.step(Action(action="click", link_id=bad_id))
    assert result.failed and result.reason.startswith("illegal_id")
    assert env.step_count == 0


def test_suite_resets_brain_when_consecutive_tasks_start_on_same_page(monkeypatch, tmp_path):
    brain = StubJev(monkeypatch)
    tasks = [{"start": "Coffee", "goal": goal, "source": "fixture"} for goal in ("Caffeine", "Stimulant")]
    rows = evaluator.run_suite(tasks, [brain], tmp_path / "results.jsonl")
    assert [r["path"] for r in rows] == [["Coffee", "Caffeine"], ["Coffee", "Stimulant"]]
    assert all(r["status"] == "success" and r["steps"] == 1 for r in rows)


class ClickFirst(Brain):
    name = "test-click"

    def choose(self, state):
        return Action(action="click", link_id=state.candidates[0].id), {}


def test_execution_exception_is_recorded_and_env_closed(monkeypatch):
    env = fixture_env()
    closed = []
    monkeypatch.setattr(evaluator, "make_env", lambda *args, **kwargs: env)
    monkeypatch.setattr(env, "close", lambda: closed.append(True))

    def fail(action):
        raise RuntimeError("simulated_navigation_failure")

    monkeypatch.setattr(env, "step", fail)
    row = evaluator.run_episode({"start": "Coffee", "goal": "Caffeine"}, ClickFirst())
    assert row["status"] == "error"
    assert row["reason"].startswith("execute:")
    assert "simulated_navigation_failure" in row["error_stack"]
    assert row["trace"][0]["error"] == row["reason"]
    assert closed == [True]


def test_page_load_diagnostics_survive_episode_failure(monkeypatch):
    env = fixture_env()
    snapshot = {"url": "https://en.wikipedia.org/wiki/Caffeine", "http_status": 503}
    browser = TwoScreenBrowser()
    browser.last_navigation = {"attempts": [{"status": "error", "page": snapshot}]}
    env.browser = browser
    monkeypatch.setattr(evaluator, "make_env", lambda *args, **kwargs: env)

    def fail(action):
        raise PageLoadError("http_503", snapshot)

    monkeypatch.setattr(env, "step", fail)
    row = evaluator.run_episode({"start": "Coffee", "goal": "Caffeine"}, ClickFirst())
    assert row["status"] == "error"
    assert row["error_diagnostics"] == snapshot
    assert row["trace"][0]["error_diagnostics"] == snapshot
    assert row["trace"][0]["navigation"]["attempts"][0]["page"] == snapshot
    assert browser.closed


def test_no_choice_or_click_after_scoring_exhausts_timeout(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(evaluator.time, "perf_counter", lambda: clock[0])

    class SlowScore(ClickFirst):
        def score_only(self, state):
            clock[0] += 2
            return [], {}

        def choose(self, state):
            pytest.fail("Choice must not run after the deadline")

    row = evaluator.run_episode(
        {"start": "Coffee", "goal": "Caffeine", "source": "fixture"}, SlowScore(), timeout_s=1
    )
    assert row["status"] == "fail" and row["reason"] == "timeout"
    assert row["steps"] == 0 and row["trace"][0]["action"] is None


def test_token_usage_does_not_count_single_batch_alias_twice():
    score = {"model": "test", "usage": {"input_tokens": 10, "output_tokens": 2}}
    choice = {"model": "test", "usage": {"input_tokens": 5, "output_tokens": 1}}
    usage = evaluator._api_usage({"score_batches": [{"score_response": score}], "score_response": score, "response": choice})
    assert usage == {"requests": 2, "attempts": 2, "input_tokens": 15, "output_tokens": 3, "models": ["test"]}


def test_browser_error_page_is_not_treated_as_empty_article(monkeypatch):
    browser = WikiBrowser()
    monkeypatch.setattr(browser, "_page_state", lambda **kw: {
        "url": "chrome-error://chromewebdata/", "error_code": "ERR_CONNECTION_RESET"
    })
    with pytest.raises(PageLoadError, match="browser_network_error") as error:
        browser._wait_ready()
    assert error.value.diagnostics["error_code"] == "ERR_CONNECTION_RESET"


def test_partial_browser_setup_is_closed(monkeypatch):
    browser = TwoScreenBrowser()

    def fail(title):
        raise RuntimeError("page_unavailable")

    browser.open_article = fail
    monkeypatch.setattr(evaluator, "WikiBrowser", lambda **kwargs: browser)
    with pytest.raises(RuntimeError, match="page_unavailable"):
        evaluator.make_env({"start": "Coffee", "goal": "Caffeine", "source": "browser"})
    assert browser.closed


def test_missing_explicit_browser_executable_fails_clearly(monkeypatch, tmp_path):
    monkeypatch.setenv("WIKIRACE_BROWSER_PATH", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError, match="WIKIRACE_BROWSER_PATH"):
        find_browser_path()


def test_jev_reuses_connection_pool_and_retries_transient_transport(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    monkeypatch.setattr("wikirace.brains.time.sleep", lambda delay: None)
    calls, clients = [], []
    real_client = httpx.Client

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("transient TLS EOF", request=request)
        return httpx.Response(200, json={"model": "test", "answers": {}})

    def client(**kwargs):
        result = real_client(transport=httpx.MockTransport(handle))
        clients.append(result)
        return result

    monkeypatch.setattr("wikirace.brains.httpx.Client", client)
    brain = JevBrain()
    assert brain._post({"questions": {}})["_transport"]["attempts"] == 2
    assert brain._post({"questions": {}})["_transport"]["attempts"] == 1
    assert len(clients) == 2  # one failed connection, one reused healthy pool
    brain.close()
    assert all(c.is_closed for c in clients)


def test_jev_does_not_retry_invalid_api_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    calls = []
    brain = JevBrain()

    def handle(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "unauthorized"})

    brain._client = httpx.Client(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            brain._post({})
        assert len(calls) == 1
    finally:
        brain.close()


def test_jev_expired_deadline_does_not_send_request(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    brain = JevBrain()
    brain.set_deadline(-1)
    with pytest.raises(TimeoutError, match="episode_deadline_exceeded"):
        brain._post({})
    assert brain._client is None
