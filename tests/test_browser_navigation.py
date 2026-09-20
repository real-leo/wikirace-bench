"""Navigation timing/transport regressions without a network or model call."""
from types import SimpleNamespace

import pytest

from wikirace.browser import PageLoadError, WikiBrowser


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("wikirace.browser.time.perf_counter", lambda: now[0])
    monkeypatch.setattr("wikirace.browser.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    return now


def article(document_id, **kwargs):
    return {"document_id": document_id, "url": "https://en.wikipedia.org/wiki/Goal",
            "article_root": True, "heading": "Goal", "ready_state": "complete",
            "http_status": 200, **kwargs}


def test_old_document_cannot_satisfy_navigation_and_slow_new_dom_is_allowed(monkeypatch, clock):
    browser = WikiBrowser(timeout=10)

    def state(**kwargs):
        if clock[0] < 1:
            return article(1, heading="Old page")
        if clock[0] < 6:  # longer than the previous fixed four-second polling loop
            return article(2, article_root=False, ready_state="loading")
        return article(2)

    monkeypatch.setattr(browser, "_page_state", state)
    assert browser._wait_ready(previous_document=1)["document_id"] == 2
    assert 6.5 <= clock[0] < 7


def test_document_still_loading_is_not_accepted_even_with_article_root(monkeypatch, clock):
    browser = WikiBrowser(timeout=1)
    monkeypatch.setattr(browser, "_page_state", lambda **kw: article(2, ready_state="loading"))
    with pytest.raises(PageLoadError, match="navigation_timeout") as exc:
        browser._wait_ready(previous_document=1)
    assert exc.value.diagnostics["ready_state"] == "loading"


def test_transient_gateway_failure_retries_only_selected_url(monkeypatch, clock):
    browser = WikiBrowser(timeout=10)
    state, gets = [article(1)], []

    def get(url, **kwargs):
        gets.append(url)
        state[0] = article(3)
        return True

    def click(script):
        state[0] = article(2, article_root=False, heading="", http_status=502)
        return True

    monkeypatch.setattr(browser, "_ensure", lambda: SimpleNamespace(get=get))
    monkeypatch.setattr(browser, "_page_state", lambda **kw: state[0])
    monkeypatch.setattr(browser, "_run_js", click)
    url = "https://en.wikipedia.org/wiki/Goal"
    browser._navigate(url, click_script="click selected link")
    assert gets == [url]
    attempts = browser.last_navigation["attempts"]
    assert [a["status"] for a in attempts] == ["error", "ok"]
    assert attempts[0]["reason"] == "http_502"


@pytest.mark.parametrize("status", [403, 404, 429])
def test_denied_missing_or_rate_limited_page_is_not_retried(monkeypatch, clock, status):
    browser = WikiBrowser()
    state, calls = [article(1)], []

    def get(url, **kwargs):
        calls.append(url)
        state[0] = article(2, http_status=status, article_root=False, heading="")
        return True  # DrissionPage get() can succeed for HTTP error documents

    monkeypatch.setattr(browser, "_ensure", lambda: SimpleNamespace(get=get))
    monkeypatch.setattr(browser, "_page_state", lambda **kw: state[0])
    with pytest.raises(PageLoadError, match=f"http_{status}"):
        browser._navigate("https://en.wikipedia.org/wiki/Goal")
    assert len(calls) == 1


def test_navigation_timeout_cannot_start_retry_after_episode_deadline(monkeypatch, clock):
    browser = WikiBrowser(timeout=30)
    browser.set_deadline(1)
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs)
        return False

    monkeypatch.setattr(browser, "_ensure", lambda: SimpleNamespace(get=get))
    monkeypatch.setattr(browser, "_page_state", lambda **kw: article(1))
    with pytest.raises(PageLoadError, match="navigation_timeout"):
        browser._navigate("https://en.wikipedia.org/wiki/Goal")
    assert len(calls) == 1
    assert calls[0]["retry"] == 0 and calls[0]["timeout"] <= 1
    assert clock[0] == pytest.approx(1)


def test_broken_javascript_context_is_preserved_in_timeout_diagnostic(monkeypatch, clock):
    browser = WikiBrowser(timeout=0.3)

    def broken(script, **kwargs):
        raise TimeoutError("driver stalled")

    monkeypatch.setattr(browser, "_run_js", broken)
    with pytest.raises(PageLoadError) as exc:
        browser._wait_ready()
    assert exc.value.diagnostics == {"diagnostic_error": "TimeoutError"}


def test_failed_initial_snapshot_cannot_disable_document_identity_check(monkeypatch, clock):
    browser = WikiBrowser(timeout=1)
    calls = []
    monkeypatch.setattr(browser, "_ensure", lambda: SimpleNamespace(get=lambda *a, **kw: calls.append(a)))
    monkeypatch.setattr(browser, "_page_state", lambda **kw: {"diagnostic_error": "TimeoutError"})
    with pytest.raises(PageLoadError, match="document_snapshot_timeout"):
        browser._navigate("https://en.wikipedia.org/wiki/Goal")
    assert calls == []
    assert browser.last_navigation["attempts"][0]["status"] == "error"
