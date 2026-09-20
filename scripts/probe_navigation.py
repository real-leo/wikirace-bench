"""Repeat a visible Wikipedia link click without calling a model."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from wikirace.browser import WikiBrowser
from wikirace.eval import _code_fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="Southeast Michigan")
    parser.add_argument("--target", default="Pontiac, Michigan")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    result = {"code_fingerprint": _code_fingerprint(), "trials": []}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with WikiBrowser() as browser:
        for trial in range(args.repeat):
            row = {"trial": trial}
            started = time.perf_counter()
            try:
                browser.open_article(args.start)
                target = next(link for link in browser.observe_links() if link.title == args.target)
                row["setup_seconds"] = round(time.perf_counter() - started, 3)
                started = time.perf_counter()
                browser.click_link(target.id)
                row["click_seconds"] = round(time.perf_counter() - started, 3)
                row["meta"] = browser.current_meta()
                row["n_viewport"] = len(browser.observe_links())
                row["status"] = "ok"
            except Exception as exc:
                row.update(status="error", error=f"{type(exc).__name__}: {exc}")
            row["navigation"] = browser.last_navigation
            result["trials"].append(row)
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
