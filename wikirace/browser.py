"""Live Wikipedia via DrissionPage: viewport-visible links only."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from DrissionPage import ChromiumOptions, ChromiumPage


def normalize_wiki_title(title: str) -> str:
    """Normalize Wikipedia titles for goal matching (spaces/underscores, trim)."""
    t = unquote(title or "").replace("_", " ").strip()
    t = re.sub(r"\s+", " ", t)
    return t


def title_from_wiki_url(url: str) -> str | None:
    """Extract article title from a Wikipedia /wiki/ URL."""
    try:
        path = urlparse(url).path
    except Exception:
        return None
    m = re.search(r"/wiki/([^#?]+)", path)
    if not m:
        return None
    title = unquote(m.group(1)).replace("_", " ")
    if ":" in title:
        ns = title.split(":", 1)[0]
        if ns in {
            "File", "Image", "Category", "Help", "Portal", "Template",
            "Special", "Talk", "User", "Wikipedia", "MediaWiki", "Draft",
            "Module", "TimedText", "Education Program", "Book",
        }:
            return None
    return title


@dataclass
class VisibleLink:
    id: str
    title: str
    href: str
    text: str


# DrissionPage run_js requires a top-level `return` to yield a value.
_VIEWPORT_LINKS_JS = r"""
return (() => {
  const root = document.querySelector('#mw-content-text')
    || document.querySelector('#bodyContent')
    || document.querySelector('main')
    || document.body;
  if (!root) return [];
  const vh = window.innerHeight || document.documentElement.clientHeight;
  const vw = window.innerWidth || document.documentElement.clientWidth;
  const seen = new Set();
  const out = [];
  const anchors = root.querySelectorAll('a[href]');
  for (const a of anchors) {
    if (a.closest('.navbox, .vertical-navbox, .toc, .mw-editsection, .reference, .noprint')) {
      continue;
    }
    const href = a.href || '';
    if (!/\/wiki\//.test(href)) continue;
    if (/\/wiki\/(File|Image|Category|Help|Portal|Template|Special|Talk|User|Wikipedia|MediaWiki|Draft|Module):/i.test(href)) {
      continue;
    }
    if (href.includes('#')) {
      const bare = href.split('#')[0];
      if (bare === location.href.split('#')[0] || bare.endsWith(location.pathname)) continue;
    }
    const rect = a.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) continue;
    const visible = rect.bottom > 0 && rect.top < vh && rect.right > 0 && rect.left < vw;
    if (!visible) continue;
    const key = href.split('#')[0];
    if (seen.has(key)) continue;
    seen.add(key);
    const text = (a.innerText || a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120);
    out.push({ href: key, text });
    if (out.length >= 80) break;
  }
  return out;
})()
"""

_PAGE_META_JS = r"""
return (() => {
  const h1 = document.querySelector('#firstHeading, h1.mw-first-heading, h1');
  const title = (h1 && (h1.innerText || h1.textContent) || document.title || '')
    .replace(/\s*-\s*Wikipedia.*$/i, '').trim();
  let extract = '';
  // Vector 2022: paragraphs may not be direct children of .mw-parser-output
  const paras = document.querySelectorAll(
    '#mw-content-text .mw-parser-output p, #mw-content-text p'
  );
  for (const p of paras) {
    const style = window.getComputedStyle(p);
    if (style && (style.display === 'none' || style.visibility === 'hidden')) continue;
    const t = (p.innerText || p.textContent || '').trim().replace(/\s+/g, ' ');
    if (t.length > 40) { extract = t; break; }
  }
  extract = extract.slice(0, 600);
  return { title, extract, url: location.href };
})()
"""


class WikiBrowser:
    """Chromium-backed Wikipedia session. Observation = viewport links only."""

    def __init__(
        self,
        lang: str = "en",
        headless: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self.lang = lang
        self.headless = headless
        self.timeout = timeout
        self._page: ChromiumPage | None = None
        self._last_candidates: list[VisibleLink] = []
        self._translated = False

    def _ensure(self) -> ChromiumPage:
        if self._page is not None:
            return self._page
        co = ChromiumOptions()
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-gpu")
        co.set_argument("--window-size=1280,900")
        try:
            co.auto_port()
        except Exception:
            pass
        if self.headless:
            try:
                co.headless(True)
            except Exception:
                co.set_argument("--headless=new")
        for path in (
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ):
            try:
                co.set_browser_path(path)
                break
            except Exception:
                continue
        self._page = ChromiumPage(addr_or_opts=co)
        try:
            self._page.set.timeouts(base=self.timeout, page_load=self.timeout)
        except Exception:
            pass
        try:
            self._page.set.window.size(1280, 900)
        except Exception:
            pass
        return self._page

    def close(self) -> None:
        if self._page is not None:
            try:
                self._page.quit()
            except Exception:
                pass
            self._page = None

    def __enter__(self) -> "WikiBrowser":
        self._ensure()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def wiki_url(self, title: str) -> str:
        slug = quote(title.replace(" ", "_"), safe=":_()/")
        return f"https://{self.lang}.wikipedia.org/wiki/{slug}"

    def open_article(self, title: str) -> None:
        page = self._ensure()
        self._translated = False
        page.get(self.wiki_url(title))
        self._wait_ready()

    def _wait_ready(self, settle: float = 0.5) -> None:
        page = self._ensure()
        try:
            page.wait.doc_loaded()
        except Exception:
            pass
        # Wait until main content exists
        for _ in range(20):
            try:
                ok = page.run_js(
                    "return !!document.querySelector('#mw-content-text, #bodyContent')"
                )
                if ok:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        time.sleep(settle)

    def current_meta(self) -> dict[str, str]:
        page = self._ensure()
        try:
            meta = page.run_js(_PAGE_META_JS)
        except Exception:
            meta = None
        if not isinstance(meta, dict):
            meta = {"title": "", "extract": "", "url": page.url or ""}
        title = normalize_wiki_title(meta.get("title") or "")
        if not title:
            title = title_from_wiki_url(meta.get("url") or page.url or "") or ""
        return {
            "title": title,
            "extract": (meta.get("extract") or "")[:600],
            "url": meta.get("url") or page.url or "",
        }

    def observe_links(self) -> list[VisibleLink]:
        page = self._ensure()
        try:
            raw = page.run_js(_VIEWPORT_LINKS_JS)
        except Exception:
            raw = []
        if not isinstance(raw, list):
            raw = []
        links: list[VisibleLink] = []
        for i, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                continue
            href = item.get("href") or ""
            title = title_from_wiki_url(href)
            if not title:
                continue
            text = (item.get("text") or title).strip() or title
            lid = f"L{i:03d}"
            links.append(VisibleLink(id=lid, title=title, href=href, text=text))
        self._last_candidates = links
        return links

    def click_link(self, link_id: str) -> VisibleLink | None:
        """Click a candidate from the last observe. Returns the link or None if illegal."""
        target = None
        for c in self._last_candidates:
            if c.id == link_id:
                target = c
                break
        if target is None:
            return None
        page = self._ensure()
        href_js = target.href.replace("\\", "\\\\").replace("'", "\\'")
        clicked = page.run_js(
            f"""
            return (() => {{
              const want = '{href_js}';
              const root = document.querySelector('#mw-content-text')
                || document.querySelector('#bodyContent') || document.body;
              const as = root.querySelectorAll('a[href]');
              for (const a of as) {{
                const h = (a.href || '').split('#')[0];
                if (h === want) {{ a.click(); return true; }}
              }}
              return false;
            }})()
            """
        )
        if not clicked:
            page.get(target.href)
        self._wait_ready(0.5)
        self._translated = False
        return target

    def scroll(self, direction: str = "down", amount: str = "page") -> None:
        page = self._ensure()
        factor = 1.0 if amount == "page" else 0.5
        sign = 1 if direction == "down" else -1
        page.run_js(
            f"""
            return (() => {{
              const h = window.innerHeight || document.documentElement.clientHeight;
              window.scrollBy(0, {sign} * h * {factor});
              return true;
            }})()
            """
        )
        time.sleep(0.3)

    def translate(self, target_lang: str) -> None:
        """Pragmatic translate: Google Translate website wrapper of the current URL.

        Documented choice (vs Wikipedia language links): keeps the same article
        content in another language without switching wiki editions / link graphs.
        """
        page = self._ensure()
        current = page.url or ""
        if "translate.google.com" in current and "u=" in current:
            qs = parse_qs(urlparse(current).query)
            current = (qs.get("u") or [current])[0]
        wrapped = (
            f"https://translate.google.com/translate?sl=auto"
            f"&tl={quote(target_lang)}&u={quote(current, safe='')}"
        )
        page.get(wrapped)
        self._wait_ready(1.0)
        self._translated = True

    @property
    def is_translated(self) -> bool:
        return self._translated
