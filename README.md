# WikiRace Bench (equal-cost scroll / click)

Viewport Wikipedia WikiRace eval. Measures whether a model can decide **when information is enough to click now** versus **scrolling for better candidates**. The **model** chooses scroll or click — the executor never decides scrolling for it (no Noul gate).

## Eval goal

Minimize total actions to reach the goal article. Scroll and click are **equal cost** in metrics (`steps` / `clicks` / `scrolls` / `seconds`). Episodes do **not** fail on step count: `--max-steps 0` (default) means unlimited; a positive value is soft info in the observation only (`remaining_steps`).

## Observation (each step)

The brain receives:

1. **Goal**: `title` + short `description`
2. **Current viewport**: visible article links in **main content only** (deduped; nav / sidebar / footer / infobox / references excluded). Each candidate: `id`, `title`, short **sentence context** around the link (not bare URL; not a full-page dump). Real `href` stays in the executor only.
3. **Candidate memory (v1)**: union of links from **current screen + previous screen** (cap only if needed; prefer keeping both). Each entry includes a rough position hint (`current_viewport|scrollY=…` or `prev_viewport`).
4. **Action state**: `path`, `step`, `max_steps` (0=unlimited), `remaining_steps` (null when unlimited), `can_scroll_down`, `can_scroll_up`

Offered click ids = viewport ∪ memory.

Example (abridged):

```json
{
  "goal": {"title": "Caffeine", "description": "Reach the Wikipedia article titled Caffeine."},
  "current": {"title": "Coffee", "description": "Coffee is a beverage…"},
  "viewport": [
    {"id": "L001", "title": "Caffeine", "context": "…contains the stimulant caffeine…", "position": "current_viewport|scrollY=0"}
  ],
  "memory": [
    {"id": "L001", "title": "Caffeine", "context": "…", "position": "current_viewport|scrollY=0"}
  ],
  "action": {
    "path": ["Coffee"],
    "step": 0,
    "max_steps": 0,
    "remaining_steps": null,
    "can_scroll_down": true,
    "can_scroll_up": false
  }
}
```

## Actions (equal cost)

| Action | Effect | Cost |
|--------|--------|------|
| `click` `link_id` | Must be in offered set (viewport ∪ memory). | **1 step** (+ recovery scrolls if off-screen) |
| `scroll` `down` / `up` | Page (or half) scroll; refreshes viewport + memory. | **1 step** |
| `translate` (optional) | Google Translate wrapper of current URL. | **1 step** |

Rules:

- Equal-cost **accounting** only: clicks + scrolls + recovery scrolls + translate all increment `steps` for reporting — they do **not** stop the episode.
- At **page bottom**: do not offer `SCROLL_DOWN` (`can_scroll_down=false`). Clicks + `SCROLL_UP` remain if allowed — this is the pressure to decide.
- At **page top**: do not offer `SCROLL_UP`.
- **Off-screen memory click**: if the model picks a remembered link not in the current viewport, the executor **auto-scrolls toward the remembered position**. **Each recovery scroll counts as one step**, then the click costs one more.
- **Termination**: (1) reached goal → success; (2) illegal action → fail; (3) wall-clock `--timeout` (default 180s, use 300s for hard races) → `reason=timeout`; (4) optional futile scroll oscillation (many consecutive alternating up/down with no click) → `reason=scroll_oscillation`. There is **no** `max_steps` fail and **no** `scroll_noop_limit`.

```json
{"action": "click", "link_id": "L001"}
{"action": "scroll", "direction": "down", "amount": "page"}
{"action": "scroll", "direction": "up", "amount": "half"}
```

## Brains / prompts

| Name | Notes |
|------|--------|
| `overlap` | Heuristic token overlap; scrolls down if best score ≤ 0 and allowed |
| `jev` | TypeSafe **Choice** over candidate ids + `SCROLL_DOWN` / `SCROLL_UP` when physically allowed |
| `gpt` / `deepseek` / `claude` | JSON action from LLM system prompt (equal-cost instructions) |

Jev / LLM instructions emphasize:

- Scroll and click each cost one step; minimize total actions.
- Best candidate need not match the goal directly — a reasonable bridge is enough.
- Only scroll if you expect clearly more valuable candidates.
- Prefer an early click on a reasonable bridge over waiting for a near-synonym.

## Metrics

Episode output:

- `status`, `path`, `steps` (total actions), `clicks`, `scrolls`, `seconds`, `reason`

`summarize()`:

- `success_rate`, `avg_steps_on_success`, `avg_clicks`, `avg_scrolls`, `avg_seconds`

## Install

```bash
cd wikirace-bench
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill TYPESAFE_API_KEY for jev; overlap needs none
```

Needs Chromium/Chrome. Headless default; use `--headed` to watch. CI / sandbox usually needs `--no-sandbox` (already set in `WikiBrowser`).

## Quick start

```bash
# Overlap heuristic
PYTHONPATH=. python run.py play \
  --brain overlap --source browser --lang en \
  --start Coffee --goal Caffeine

# Jev (TypeSafe Choice)
PYTHONPATH=. python run.py play \
  --brain jev --source browser \
  --start "Rubber duck" --goal Bathing --timeout 180

# Harder race (higher wall-clock safety timeout)
PYTHONPATH=. python run.py play \
  --brain jev --source browser \
  --start Tea --goal Moon --timeout 300
```

Batch:

```bash
PYTHONPATH=. python run.py bench --brains overlap,jev --source browser --lang en
```

## CLI

```
run.py play|bench
  --source browser|fixture|live
  --lang en
  --headless / --headed
  --brain / --brains
  --start --goal --max-steps 0
  --timeout 180
```

## Directory

```
wikirace-bench/
  run.py
  requirements.txt
  .env.example
  data/tasks.json
  data/fixture_wiki.json
  wikirace/
    browser.py   # DrissionPage + viewport links + sentence context
    state.py     # RaceState / Action / Candidate
    env.py       # RaceEnv (equal-cost, memory, recovery scrolls)
    brains.py
    wiki.py
    eval.py
```

## Design notes

- **Stable link ids** within a page visit (href → `L001`…); reused across scrolls so memory ids stay valid.
- Fixture / live MediaWiki API modes still expose a full-page link list with no real scroll (`can_scroll_*=false`).
- Baidu Baike remains a TODO stub.
