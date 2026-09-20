from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "data" / "fixture_wiki.json"

USER_AGENT = "wikirace-bench/0.2 (research; contact: local)"

# Same length cap for goal descriptions and page intro extracts.
EXTRACT_CHARS = 280


def fetch_intro_extract(title: str, lang: str = "en", max_chars: int = EXTRACT_CHARS) -> str:
    """Wikipedia intro extract via MediaWiki API (plain text, length-capped)."""
    if not title:
        return ""
    api = f"https://{lang}.wikipedia.org/w/api.php"
    try:
        with httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT}) as client:
            r = client.get(
                api,
                params={
                    "action": "query",
                    "format": "json",
                    "prop": "extracts",
                    "exintro": 1,
                    "explaintext": 1,
                    "titles": title,
                },
            )
            r.raise_for_status()
            pages = r.json()["query"]["pages"]
            page = next(iter(pages.values()))
            text = (page.get("extract") or "").strip().replace("\n", " ")
            text = re.sub(r"\s+", " ", text)
            return text[:max_chars]
    except Exception:
        return ""


class WikiSource:
    def get(self, title: str) -> tuple[str, list[str]]:
        raise NotImplementedError


class FixtureWiki(WikiSource):
    """Offline graph for quick tests without a browser."""

    def __init__(self, path: Path = FIXTURE_PATH) -> None:
        self.pages: dict[str, dict] = json.loads(path.read_text())

    def get(self, title: str) -> tuple[str, list[str]]:
        if title not in self.pages:
            raise KeyError(
                f"fixture missing page {title!r}; use source=browser for live Wikipedia"
            )
        page = self.pages[title]
        links = [t for t in page["links"] if t in self.pages and t != title]
        return page["extract"], links


class LiveWikipedia(WikiSource):
    """Legacy MediaWiki API: full-page links (no viewport). Prefer browser mode."""

    def __init__(self, lang: str = "en", timeout: float = 20.0) -> None:
        self.api = f"https://{lang}.wikipedia.org/w/api.php"
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})

    def get(self, title: str) -> tuple[str, list[str]]:
        extract = self._extract(title)
        links = self._links(title)
        return extract, links

    def _extract(self, title: str) -> str:
        r = self.client.get(
            self.api,
            params={
                "action": "query",
                "format": "json",
                "prop": "extracts",
                "exintro": 1,
                "explaintext": 1,
                "titles": title,
            },
        )
        r.raise_for_status()
        pages = r.json()["query"]["pages"]
        page = next(iter(pages.values()))
        text = (page.get("extract") or "").strip().replace("\n", " ")
        return text[:600]

    def _links(self, title: str) -> list[str]:
        titles: list[str] = []
        plcontinue = None
        while True:
            params = {
                "action": "query",
                "format": "json",
                "prop": "links",
                "plnamespace": 0,
                "pllimit": "max",
                "titles": title,
            }
            if plcontinue:
                params["plcontinue"] = plcontinue
            r = self.client.get(self.api, params=params)
            r.raise_for_status()
            data = r.json()
            pages = data["query"]["pages"]
            page = next(iter(pages.values()))
            for item in page.get("links") or []:
                t = item["title"]
                if t != title and t not in titles:
                    titles.append(t)
            if "continue" in data and "plcontinue" in data["continue"]:
                plcontinue = data["continue"]["plcontinue"]
            else:
                break
        return titles


class BaiduBaikeSource(WikiSource):
    """TODO: Baidu Baike adapter (not implemented).

    Planned: browser or HTML scrape of baike.baidu.com with viewport-visible
    internal links, same Action schema as Wikipedia browser mode.
    """

    def get(self, title: str) -> tuple[str, list[str]]:
        raise NotImplementedError(
            "Baidu Baike source is a TODO stub. Use source=browser for Wikipedia."
        )
