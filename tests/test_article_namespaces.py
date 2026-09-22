import pytest

from wikirace.browser import WikiBrowser, title_from_wiki_url


@pytest.mark.parametrize("path", [
    "Template_talk:Science_and_technology_studies", "User_talk:DASonnenfeld",
    "User_talk:DASonnenfeld/Archive_1", "Template%20talk%3AScience", "uSeR_tAlK%3ABob",
    "File_talk:Photo.jpg", "Category_talk:Science", "Wikipedia_talk:Policy",
    "WP:Policy", "WT:Policy", "Project:Policy", "Media:Photo.jpg", "Help:Editing",
])
def test_non_article_namespaces_and_aliases_are_filtered(path):
    assert title_from_wiki_url("https://en.wikipedia.org/wiki/" + path) is None


@pytest.mark.parametrize("title", ["Star_Trek:_Voyager", "2001:_A_Space_Odyssey", "Coffee", "Talk_radio"])
def test_colons_in_real_article_titles_are_preserved(title):
    assert title_from_wiki_url("https://en.wikipedia.org/wiki/" + title) == title.replace("_", " ")


@pytest.mark.parametrize("query", [
    "action=edit&redlink=1", "redlink=1", "redlink=%31", "action=edit",
    "action=view&action=edit", "veaction=edit", "action=history", "redlink=",
])
def test_missing_article_and_editor_links_are_not_candidates(query):
    assert title_from_wiki_url("https://en.wikipedia.org/wiki/Tuping?" + query) is None


@pytest.mark.parametrize("query", ["action=view", "oldid=123", "useskin=vector"])
def test_normal_article_view_queries_are_preserved(query):
    assert title_from_wiki_url("https://en.wikipedia.org/wiki/Coffee?" + query) == "Coffee"


def test_python_collection_guard_rejects_redlinks_even_if_dom_prefilter_misses_them(monkeypatch):
    browser = WikiBrowser()
    monkeypatch.setattr(browser, "_ensure", lambda: object())
    monkeypatch.setattr(browser, "scroll_metrics", lambda: {"y": 0})
    monkeypatch.setattr(browser, "_run_js", lambda *a, **kw: [
        {"href": "https://en.wikipedia.org/wiki/Tuping?action=edit&redlink=1", "text": "Tuping"},
        {"href": "https://en.wikipedia.org/wiki/Topeng", "text": "Topeng"}])
    links = browser.observe_links(scope="page")
    assert [link.title for link in links] == ["Topeng"]
    assert len(browser._registry) == 1
