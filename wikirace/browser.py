"""Live Wikipedia via DrissionPage: viewport-visible links with sentence context."""
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
    context: str = ""
    scroll_y: float = 0.0
    abs_y: float = 0.0  # document Y of link top


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
  const scrollY = window.scrollY || window.pageYOffset || 0;
  const seen = new Set();
  const out = [];
  const anchors = root.querySelectorAll('a[href]');
  for (const a of anchors) {
    if (a.closest(
      'nav, .navbox, .vertical-navbox, .toc, .mw-editsection, .reference, '
      + '.noprint, .sidebar, .infobox, .hatnote, .metadata, footer, #footer, '
      + '#mw-navigation, #mw-panel, #mw-head, .vector-header, .vector-toc, '
      + '.mw-footer, .catlinks'
    )) {
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
    // Sentence-ish context: surrounding block text, clipped around the link
    let context = '';
    const block = a.closest('p, li, dd, td, th, blockquote, h1, h2, h3, h4, h5, h6') || a.parentElement;
    if (block) {
      const full = (block.innerText || block.textContent || '').trim().replace(/\s+/g, ' ');
      if (full.length <= 280) {
        context = full;
      } else {
        const needle = text || '';
        const idx = needle ? full.indexOf(needle) : -1;
        if (idx >= 0) {
          const start = Math.max(0, idx - 100);
          const end = Math.min(full.length, idx + needle.length + 140);
          context = (start > 0 ? '…' : '') + full.slice(start, end) + (end < full.length ? '…' : '');
        } else {
          context = full.slice(0, 240) + (full.length > 240 ? '…' : '');
        }
      }
    }
    if (!context) context = text;
    out.push({
      href: key,
      text,
      context: context.slice(0, 280),
      abs_y: rect.top + scrollY,
    });
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
        # Stable id assignment within a page visit (href -> id)
        self._href_to_id: dict[str, str] = {}
        self._next_id: int = 1
        # Internal registry for memory recall (id -> VisibleLink-like data)
        self._registry: dict[str, VisibleLink] = {}

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
        self._href_to_id.clear()
        self._registry.clear()
        self._next_id = 1
        self._last_candidates = []
        page.get(self.wiki_url(title))
        self._wait_ready()

    def _wait_ready(self, settle: float = 0.5) -> None:
        page = self._ensure()
        try:
            page.wait.doc_loaded()
        except Exception:
            pass
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

    def _stable_id(self, href: str) -> str:
        if href in self._href_to_id:
            return self._href_to_id[href]
        lid = f"L{self._next_id:03d}"
        self._next_id += 1
        self._href_to_id[href] = lid
        return lid

    def observe_links(self) -> list[VisibleLink]:
        page = self._ensure()
        metrics = self.scroll_metrics()
        scroll_y = float(metrics.get("y") or 0)
        try:
            raw = page.run_js(_VIEWPORT_LINKS_JS)
        except Exception:
            raw = []
        if not isinstance(raw, list):
            raw = []
        links: list[VisibleLink] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            href = item.get("href") or ""
            title = title_from_wiki_url(href)
            if not title:
                continue
            text = (item.get("text") or title).strip() or title
            context = (item.get("context") or text).strip() or text
            abs_y = float(item.get("abs_y") or scroll_y)
            lid = self._stable_id(href)
            vl = VisibleLink(
                id=lid,
                title=title,
                href=href,
                text=text,
                context=context,
                scroll_y=scroll_y,
                abs_y=abs_y,
            )
            links.append(vl)
            self._registry[lid] = vl
        self._last_candidates = links
        return links

    def registry_get(self, link_id: str) -> VisibleLink | None:
        return self._registry.get(link_id)

    def link_in_viewport(self, link_id: str) -> bool:
        return any(c.id == link_id for c in self._last_candidates)

    def click_link(self, link_id: str) -> VisibleLink | None:
        """Click a candidate currently in the viewport (or registered href)."""
        target = None
        for c in self._last_candidates:
            if c.id == link_id:
                target = c
                break
        if target is None:
            target = self._registry.get(link_id)
        if target is None:
            return None
        return self._click_href(target)

    def _click_href(self, target: VisibleLink) -> VisibleLink:
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
        # New page: reset id registry
        self._href_to_id.clear()
        self._registry.clear()
        self._next_id = 1
        self._last_candidates = []
        return target

    def scroll_toward_link(self, link_id: str) -> dict[str, float | bool | str]:
        """One page-scroll toward a remembered link's abs_y. Counts as one scroll.

        Returns metrics including whether the link is now in the viewport.
        """
        target = self._registry.get(link_id)
        if target is None:
            return {"changed": False, "in_viewport": False, "reason": "unknown_id"}
        metrics = self.scroll_metrics()
        cur_y = float(metrics["y"])
        vh = 0.0
        page = self._ensure()
        try:
            vh = float(
                page.run_js(
                    "return window.innerHeight || document.documentElement.clientHeight || 0"
                )
                or 0
            )
        except Exception:
            vh = 900.0
        target_y = float(target.abs_y)
        # Aim so link is roughly in upper half of viewport
        desired = max(0.0, target_y - vh * 0.25)
        if abs(desired - cur_y) < 40:
            # Already near; try a tiny nudge then re-observe
            direction = "down" if desired >= cur_y else "up"
            result = self.scroll(direction=direction, amount="half")
        elif desired > cur_y:
            result = self.scroll(direction="down", amount="page")
        else:
            result = self.scroll(direction="up", amount="page")
        # Re-observe to refresh last_candidates
        self.observe_links()
        in_view = self.link_in_viewport(link_id)
        result["in_viewport"] = in_view
        result["desired_y"] = desired
        return result

    def scroll_metrics(self) -> dict[str, float | bool]:
        """Current scrollY and whether the viewport is at top/bottom."""
        page = self._ensure()
        try:
            raw = page.run_js(
                """
                return (() => {
                  const y = window.scrollY || window.pageYOffset || 0;
                  const h = window.innerHeight || document.documentElement.clientHeight || 0;
                  const sh = Math.max(
                    document.documentElement.scrollHeight || 0,
                    document.body ? document.body.scrollHeight : 0
                  );
                  const maxY = Math.max(0, sh - h);
                  const at_bottom = y >= maxY - 2;
                  const at_top = y <= 2;
                  return { y, maxY, at_bottom, at_top, vh: h };
                })()
                """
            )
        except Exception:
            raw = None
        if not isinstance(raw, dict):
            return {"y": 0.0, "maxY": 0.0, "at_bottom": True, "at_top": True, "vh": 900.0}
        return {
            "y": float(raw.get("y") or 0),
            "maxY": float(raw.get("maxY") or 0),
            "at_bottom": bool(raw.get("at_bottom")),
            "at_top": bool(raw.get("at_top")),
            "vh": float(raw.get("vh") or 900),
        }

    def scroll(self, direction: str = "down", amount: str = "page") -> dict[str, float | bool]:
        """Scroll the page. Returns metrics including whether position changed."""
        page = self._ensure()
        before = self.scroll_metrics()
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
        after = self.scroll_metrics()
        changed = abs(float(after["y"]) - float(before["y"])) > 1.0
        return {
            "y_before": before["y"],
            "y_after": after["y"],
            "maxY": after["maxY"],
            "changed": changed,
            "at_bottom": after["at_bottom"],
            "at_top": after["at_top"],
        }

    def translate(self, target_lang: str) -> None:
        """Optional: Google Translate website wrapper of the current URL."""
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
        self._href_to_id.clear()
        self._registry.clear()
        self._next_id = 1
        self._last_candidates = []

    @property
    def is_translated(self) -> bool:
        return self._translated
