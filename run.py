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
def play(
    brain: str,
    start: str,
    goal: str,
    source: str,
    max_steps: int,
    lang: str,
    headless: bool,
) -> None:
    task = {
        "id": "adhoc",
        "source": source,
        "start": start,
        "goal": goal,
        "max_steps": max_steps,
    }
    row = run_episode(task, build_brain(brain), lang=lang, headless=headless)
    print(json.dumps({k: row[k] for k in row if k != "trace"}, ensure_ascii=False, indent=2))
    print("path:", " → ".join(row["path"]))
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
def bench(
    brains: str,
    tasks: str,
    out: str,
    lang: str,
    headless: bool,
    source: str | None,
) -> None:
    task_list = json.loads(Path(tasks).read_text())
    if source:
        for t in task_list:
            t["source"] = source
    brain_list = [build_brain(name.strip()) for name in brains.split(",") if name.strip()]
    rows = run_suite(task_list, brain_list, Path(out), lang=lang, headless=headless)
    by_brain: dict[str, list] = {}
    for row in rows:
        by_brain.setdefault(row["brain"], []).append(row)
    print("\n== summary ==")
    for name, group in by_brain.items():
        print(name, summarize(group))


if __name__ == "__main__":
    cli()
