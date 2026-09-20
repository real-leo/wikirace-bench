"""Live Wikipedia via DrissionPage: viewport-visible links with sentence context."""
from __future__ import annotations

import re
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage.errors import ContextLostError


class PageLoadError(RuntimeError):
    """A navigation failure with a compact, serializable browser snapshot."""

    def __init__(self, reason: str, diagnostics: dict) -> None:
        self.reason = reason
        self.diagnostics = diagnostics
        super().__init__(f"wikipedia_page_unavailable:{reason}:{diagnostics.get('url', '')}")


def find_browser_path() -> str | None:
    """Use an explicit executable, then an installed Chrome/Chromium binary."""
    explicit = os.environ.get("WIKIRACE_BROWSER_PATH")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"WIKIRACE_BROWSER_PATH does not exist: {path}")
        return str(path)
    candidates = [
        shutil.which(name)
        for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")
    ]
    candidates += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    return next((p for p in candidates if p and Path(p).is_file()), None)


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
  const allPage = __ALL_PAGE__;
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
    if (!allPage && a.closest(
      'nav, .navbox, .vertical-navbox, .toc, .mw-editsection, .reference, '
      + '.noprint, .sidebar, .infobox, .hatnote, .metadata, footer, #footer, '
      + '#mw-navigation, #mw-panel, #mw-head, .vector-header, .vector-toc, '
      + '.mw-footer, .catlinks'
    )) {
      continue;
    }
    const href = a.href || '';
    let parsed;
    try { parsed = new URL(href); } catch { continue; }
    if (parsed.origin !== location.origin) continue;
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
    if (typeof a.checkVisibility === 'function' &&
        !a.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    const visible = rect.bottom > 0 && rect.top < vh && rect.right > 0 && rect.left < vw;
    if (!allPage && !visible) continue;
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
    if (!allPage && out.length >= 80) break;
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

_PAGE_STATE_JS = r"""
return (() => {
  const root = document.querySelector('#mw-content-text, #bodyContent');
  const heading = document.querySelector('#firstHeading, h1.mw-first-heading');
  const nav = performance.getEntriesByType('navigation')[0];
  return {
    url: location.href, title: document.title, ready_state: document.readyState,
    document_id: performance.timeOrigin, article_root: !!root,
    heading: (heading?.textContent || '').trim(),
    http_status: nav?.responseStatus || null,
    error_code: (document.querySelector('.error-code')?.textContent || '').trim(),
    body_excerpt: root ? '' : (document.body?.innerText || '').slice(0, 500)
  };
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
        self._deadline: float | None = None
        self.last_navigation: dict = {}

    def _ensure(self) -> ChromiumPage:
        if self._page is not None:
            return self._page
        co = ChromiumOptions()
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-gpu")
        co.set_argument("--window-size=1280,900")
        # Readiness below waits for a new, parsed article. Do not make get()
        # wait for unrelated images/analytics or stop their loading (eager).
        co.set_load_mode("none")
        try:
            co.auto_port()
        except Exception:
            pass
        if self.headless:
            try:
                co.headless(True)
            except Exception:
                co.set_argument("--headless=new")
        browser_path = find_browser_path()
        if browser_path:
            co.set_browser_path(browser_path)
        proxy = os.environ.get("WIKIRACE_BROWSER_PROXY")
        if proxy:
            co.set_argument("--proxy-server", proxy)
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

    def set_deadline(self, deadline: float | None) -> None:
        self._deadline = deadline

    def _run_js(self, script: str, timeout: float = 10.0):
        # Runtime.evaluate uses the current execution context. The default
        # callFunctionOn binds to DrissionPage's cached document object, which
        # can belong to the previous page while navigation callbacks catch up.
        if self._deadline is not None:
            remaining = self._deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError("episode_deadline_exceeded")
            timeout = min(timeout, remaining)
        return self._ensure().run_js(
            f"(() => {{ {script} }})()", as_expr=True, timeout=timeout
        )

    def _page_state(self, timeout: float = 2.0) -> dict:
        try:
            state = self._run_js(_PAGE_STATE_JS, timeout=timeout)
            if isinstance(state, dict):
                return state
        except Exception as exc:
            return {"diagnostic_error": type(exc).__name__}
        return {"diagnostic_error": "invalid_browser_snapshot"}

    def open_article(self, title: str) -> None:
        page = self._ensure()
        self._translated = False
        self._href_to_id.clear()
        self._registry.clear()
        self._next_id = 1
        self._last_candidates = []
        self._navigate(self.wiki_url(title))

    def _wait_ready(
        self, settle: float = 0.5, *, previous_document: float | None = None,
        deadline: float | None = None,
    ) -> dict:
        deadline = deadline if deadline is not None else time.perf_counter() + self.timeout
        stable_since = None
        stable_document = None
        state: dict = {}
        while time.perf_counter() < deadline:
            state = self._page_state(timeout=min(2.0, max(0.01, deadline - time.perf_counter())))
            changed = previous_document is None or state.get("document_id") != previous_document
            status = state.get("http_status") or 0
            if changed and (status >= 400 or state.get("error_code") or
                            str(state.get("url", "")).startswith("chrome-error:")):
                reason = f"http_{status}" if status >= 400 else "browser_network_error"
                raise PageLoadError(reason, state)
            ready = (changed and state.get("ready_state") in ("interactive", "complete")
                     and state.get("article_root") and state.get("heading"))
            if ready:
                if stable_document != state.get("document_id"):
                    stable_since = time.perf_counter()
                    stable_document = state.get("document_id")
                if stable_since is not None and time.perf_counter() - stable_since >= settle:
                    return state
            else:
                stable_since = stable_document = None
            time.sleep(min(0.1, max(0.0, deadline - time.perf_counter())))
        raise PageLoadError("navigation_timeout", state)

    def _navigate(self, url: str, click_script: str | None = None, settle: float = 0.5) -> None:
        page = self._ensure()
        self.last_navigation = {"requested_url": url, "attempts": []}
        for attempt in range(2):
            started = time.perf_counter()
            deadline = started + self.timeout
            if self._deadline is not None:
                deadline = min(deadline, self._deadline)
            if deadline <= started:
                raise PageLoadError("episode_deadline", {"url": url})
            record = {"attempt": attempt + 1}
            self.last_navigation["attempts"].append(record)
            try:
                # A failed snapshot must not turn off the new-document check.
                # Wait for the old document's identity BEFORE issuing a click.
                while True:
                    before = self._page_state(timeout=min(2.0, max(0.01, deadline - time.perf_counter())))
                    if isinstance(before.get("document_id"), (int, float)):
                        break
                    if time.perf_counter() >= deadline:
                        raise PageLoadError("document_snapshot_timeout", {"url": url, **before})
                    time.sleep(min(0.1, max(0.0, deadline - time.perf_counter())))
                record["previous_document"] = before["document_id"]
                if time.perf_counter() >= deadline:
                    raise PageLoadError("navigation_timeout", before)
                clicked = False
                if attempt == 0 and click_script:
                    try:
                        clicked = self._run_js(click_script)
                    except ContextLostError:
                        # The click can commit a navigation before its JS reply.
                        clicked = True
                if not clicked:
                    record["get_returned"] = page.get(
                        url, retry=0, timeout=max(0.01, deadline - time.perf_counter())
                    )
                state = self._wait_ready(
                    settle, previous_document=before.get("document_id"), deadline=deadline
                )
                record.update(status="ok", page=state)
                return
            except PageLoadError as exc:
                record.update(status="error", reason=exc.reason, page=exc.diagnostics)
                # Do not retry access denials, missing articles, or arbitrary
                # HTTP failures. A retry only reloads the SAME selected link.
                retryable = exc.reason in {"navigation_timeout", "http_500", "http_502", "http_503", "http_504"}
                if exc.reason == "browser_network_error":
                    retryable = exc.diagnostics.get("error_code") in {
                        "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_TIMED_OUT",
                        "ERR_CONNECTION_TIMED_OUT", "ERR_NETWORK_CHANGED", "ERR_EMPTY_RESPONSE",
                    }
                if not retryable or attempt == 1 or (self._deadline is not None and time.perf_counter() >= self._deadline):
                    raise
            finally:
                record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            pause = 0.5 if self._deadline is None else min(0.5, max(0, self._deadline - time.perf_counter()))
            time.sleep(pause)

    def current_meta(self) -> dict[str, str]:
        page = self._ensure()
        try:
            meta = self._run_js(_PAGE_META_JS)
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

    def observe_links(self, scope: str = "viewport") -> list[VisibleLink]:
        if scope not in {"viewport", "page"}:
            raise ValueError(f"unknown_link_scope:{scope}")
        page = self._ensure()
        metrics = self.scroll_metrics()
        scroll_y = float(metrics.get("y") or 0)
        try:
            raw = self._run_js(
                _VIEWPORT_LINKS_JS.replace("__ALL_PAGE__", "true" if scope == "page" else "false"),
                timeout=self.timeout if scope == "page" else 10.0,
            )
        except Exception:
            if scope == "page":
                raise  # Extraction failure is not a page with no links.
            raw = []
        if not isinstance(raw, list):
            if scope == "page":
                raise RuntimeError("invalid_article_links_snapshot")
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
        click_script = (
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
        self._navigate(target.href, click_script=click_script)
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
                self._run_js(
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
            raw = self._run_js(
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
        self._run_js(
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
        self._navigate(wrapped, settle=1.0)
        self._translated = True
        self._href_to_id.clear()
        self._registry.clear()
        self._next_id = 1
        self._last_candidates = []

    @property
    def is_translated(self) -> bool:
        return self._translated
