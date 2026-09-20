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
from wikirace.eval import DEFAULT_TIMEOUT_S, run_episode, run_suite, summarize

load_dotenv(ROOT / ".env")


@click.group()
def cli() -> None:
    """Wikipedia WikiRace: whole-page links by default; optional viewport mode."""


@cli.command()
@click.option("--brain", default="overlap", help="overlap,jev,laya,gpt,claude,deepseek")
@click.option("--start", default="Coffee")
@click.option("--goal", default="Caffeine")
@click.option(
    "--source",
    default="browser",
    type=click.Choice(["browser", "fixture", "live"]),
    help="browser=DrissionPage (primary); fixture=offline; live=MediaWiki API",
)
@click.option(
    "--max-steps",
    default=0,
    type=int,
    show_default=True,
    help="Soft step budget shown in observation only (0=unlimited). Never fails the episode.",
)
@click.option("--lang", default="en", help="Wikipedia language code")
@click.option("--out", default=None, type=click.Path(dir_okay=False), help="Save the full episode JSON, including decision evidence.")
@click.option("--headless/--headed", default=True, help="Chromium headless (default true)")
@click.option("--observation", "observation_mode", default="page", type=click.Choice(["page", "viewport"]), show_default=True, help="page=all rendered article links, click only (Jev/overlap); viewport=scroll/click")
@click.option(
    "--timeout",
    default=DEFAULT_TIMEOUT_S,
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
    out: str | None,
    observation_mode: str,
) -> None:
    task = {
        "id": "adhoc",
        "source": source,
        "start": start,
        "goal": goal,
        "max_steps": max_steps,
        "observation_mode": observation_mode,
    }
    timeout_s = None if timeout <= 0 else timeout
    row = run_episode(
        task, build_brain(brain), lang=lang, headless=headless, timeout_s=timeout_s
    )
    if out:
        output = Path(out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: row[k] for k in row if k != "trace"}, ensure_ascii=False, indent=2))
    print("path:", " → ".join(row["path"]))
    print(
        f"steps={row.get('steps')} clicks={row.get('clicks')} "
        f"scrolls={row.get('scrolls')} seconds={row.get('seconds')}"
    )
    if row.get("trace"):
        print("actions:")
        for t in row["trace"]:
            print(
                f"  step {t['step']}: {t.get('action')} -> {t.get('chosen_title')} "
                f"(consumed={t.get('actions_consumed')})"
            )


@cli.command()
@click.option("--brains", default="overlap", help="comma list: overlap,jev,laya,gpt,claude,deepseek")
@click.option("--tasks", default=str(ROOT / "data" / "tasks.json"))
@click.option("--out", default=str(ROOT / "runs" / "latest.jsonl"))
@click.option("--lang", default="en")
@click.option("--headless/--headed", default=True)
@click.option("--observation", "observation_mode", default="page", type=click.Choice(["page", "viewport"]), show_default=True)
@click.option(
    "--source",
    default=None,
    type=click.Choice(["browser", "fixture", "live"]),
    help="Override task source for all tasks",
)
@click.option(
    "--timeout",
    default=DEFAULT_TIMEOUT_S,
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
    observation_mode: str,
) -> None:
    task_list = json.loads(Path(tasks).read_text())
    for task in task_list:
        task["observation_mode"] = observation_mode
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
