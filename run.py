#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wikirace.brains import build_brain
from wikirace.eval import run_episode, run_suite, summarize

load_dotenv(ROOT / ".env")


@click.group()
def cli() -> None:
    """Wikipedia WikiRace bench (DrissionPage viewport links)."""


@cli.command()
@click.option("--brain", default="overlap", help="overlap,jev,gpt,claude,deepseek")
@click.option("--start", default="Coffee")
@click.option("--goal", default="Caffeine")
@click.option(
    "--source",
    default="browser",
    type=click.Choice(["browser", "fixture", "live"]),
    help="browser=DrissionPage (primary); fixture=offline; live=MediaWiki API",
)
@click.option("--max-steps", default=12, type=int)
@click.option("--lang", default="en", help="Wikipedia language code")
@click.option("--headless/--headed", default=True, help="Chromium headless (default true)")
@click.option(
    "--timeout",
    default=120.0,
    type=float,
    show_default=True,
    help="Wall-clock episode timeout in seconds (fail reason=timeout). 0=disable.",
)
def play(
    brain: str,
    start: str,
    goal: str,
    source: str,
    max_steps: int,
    lang: str,
    headless: bool,
    timeout: float,
) -> None:
    task = {
        "id": "adhoc",
        "source": source,
        "start": start,
        "goal": goal,
        "max_steps": max_steps,
    }
    timeout_s = None if timeout <= 0 else timeout
    row = run_episode(
        task, build_brain(brain), lang=lang, headless=headless, timeout_s=timeout_s
    )
    print(json.dumps({k: row[k] for k in row if k != "trace"}, ensure_ascii=False, indent=2))
    print("path:", " → ".join(row["path"]))
    print(
        f"nav_steps={row.get('steps')} scrolls={row.get('scrolls')} "
        f"seconds={row.get('seconds')}"
    )
    if row.get("trace"):
        print("actions:")
        for t in row["trace"]:
            print(f"  step {t['step']}: {t.get('action')} -> {t.get('chosen_title')}")


@cli.command()
@click.option("--brains", default="overlap", help="comma list: overlap,jev,gpt,claude,deepseek")
@click.option("--tasks", default=str(ROOT / "data" / "tasks.json"))
@click.option("--out", default=str(ROOT / "runs" / "latest.jsonl"))
@click.option("--lang", default="en")
@click.option("--headless/--headed", default=True)
@click.option(
    "--source",
    default=None,
    type=click.Choice(["browser", "fixture", "live"]),
    help="Override task source for all tasks",
)
@click.option(
    "--timeout",
    default=120.0,
    type=float,
    show_default=True,
    help="Wall-clock episode timeout in seconds (fail reason=timeout). 0=disable.",
)
def bench(
    brains: str,
    tasks: str,
    out: str,
    lang: str,
    headless: bool,
    source: str | None,
    timeout: float,
) -> None:
    task_list = json.loads(Path(tasks).read_text())
    if source:
        for t in task_list:
            t["source"] = source
    brain_list = [build_brain(name.strip()) for name in brains.split(",") if name.strip()]
    timeout_s = None if timeout <= 0 else timeout
    rows = run_suite(
        task_list,
        brain_list,
        Path(out),
        lang=lang,
        headless=headless,
        timeout_s=timeout_s,
    )
    by_brain: dict[str, list] = {}
    for row in rows:
        by_brain.setdefault(row["brain"], []).append(row)
    print("\n== summary ==")
    for name, group in by_brain.items():
        print(name, summarize(group))


if __name__ == "__main__":
    cli()
