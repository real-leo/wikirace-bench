# WikiRace Bench (scroll-down + scored finalists)

Viewport Wikipedia WikiRace eval. Measures whether a model can decide **when information is enough to click now** versus **scrolling for better candidates**, with **graded bridge-relevance scores** and a forced **finalist pick** at page bottom.

The **model** chooses scroll-down or click — the executor never decides scrolling for it (no Noul gate). **SCROLL_UP is not offered** (recovery scrolls for off-screen memory clicks remain executor-only).

## Eval goal

Minimize total actions to reach the goal article. Scroll-down and click are **equal cost** in metrics (`steps` / `clicks` / `scrolls` / `seconds`). Episodes do **not** fail on step count: `--max-steps 0` (default) means unlimited; a positive value is soft info in the observation only (`remaining_steps`).

## Observation (each step)

The brain receives:

1. **Goal**: `title` + short `description` (Wikipedia intro extract, same source/length as page extracts — not a title echo)
2. **Current viewport**: visible article links in **main content only** (deduped; nav / sidebar / footer / infobox / references excluded). Each candidate: `id`, `title`, short **sentence context**, optional `score`. Real `href` stays in the executor only.
3. **Candidate memory (v1)**: union of links from **current screen + previous screen**.
4. **Action state**: `path`, `step`, `max_steps` (0=unlimited), `remaining_steps` (null when unlimited), `can_scroll_down`, `page_scroll_count`, `finalist_mode`, `finalist_k`
5. **Finalists** (when `finalist_mode`): top-K highest-scored links seen while on the **current page**

Offered click ids = viewport ∪ memory in normal mode, or **only top-K finalists** at page bottom.

Example (abridged):

```json
{
  "goal": {"title": "Caffeine", "description": "A central nervous system stimulant of the methylxanthine class…"},
  "current": {"title": "Coffee", "description": "Coffee is a beverage…"},
  "viewport": [
    {"id": "L001", "title": "Caffeine", "context": "…contains the stimulant caffeine…", "position": "current_viewport|scrollY=0", "score": 0.95}
  ],
  "memory": [
    {"id": "L001", "title": "Caffeine", "context": "…", "position": "current_viewport|scrollY=0", "score": 0.95}
  ],
  "finalists": [],
  "action": {
    "path": ["Coffee"],
    "step": 0,
    "max_steps": 0,
    "remaining_steps": null,
    "can_scroll_down": true,
    "page_scroll_count": 0,
    "finalist_mode": false,
    "finalist_k": 5
  }
}
```

## Relevance scoring

Each step, brains score visible candidates as **bridges toward the goal** (not writing quality):

| Level | Meaning |
|-------|---------|
| 0 | Irrelevant / misleading |
| 1 | Weak / tangential |
| 2 | Reasonable bridge |
| 3 | Strong bridge |
| 4 | Direct / near-direct / is the goal |

Scores are normalized to **0–1** and persisted per link title (keep **highest** across the episode; page book resets on navigation). Jev uses TypeSafe **Score** (batch per viewport). Overlap uses token-overlap heuristics on the same scale.

## Actions (equal cost)

| Action | Effect | Cost |
|--------|--------|------|
| `click` `link_id` | Must be in the offered set. | **1 step** (+ recovery scrolls if off-screen) |
| `scroll` `down` | Page (or half) scroll; refreshes viewport + memory. | **1 step** |
| `translate` (optional) | Google Translate wrapper of current URL. | **1 step** |

Rules:

- **No SCROLL_UP** for the model. Executor may still scroll up when recovering an off-screen memory click.
- Equal-cost **accounting** only: clicks + scrolls + recovery scrolls + translate all increment `steps` for reporting — they do **not** stop the episode.
- At **page bottom** (`can_scroll_down=false`): enter **finalist mode**. Observation includes `page_scroll_count` and **top-K** (default K=5) highest-scored links seen on this page. Choice is **only** among those ids (no scroll). If top-K is empty → fail `finalist_empty`.
- **Off-screen memory click**: executor auto-scrolls toward the remembered position; each recovery scroll counts as one step, then the click costs one more.
- **Termination**: (1) reached goal → success; (2) illegal action → fail; (3) wall-clock `--timeout` (default 180s; use 300s for hard races) → `reason=timeout`; (4) `finalist_empty`. There is **no** `max_steps` fail and **no** scroll-oscillation detector.

```json
{"action": "click", "link_id": "L001"}
{"action": "scroll", "direction": "down", "amount": "page"}
```

## Brains / prompts

| Name | Notes |
|------|--------|
| `overlap` | Heuristic bridge scores; scrolls down if best is near-zero; at bottom clicks best of top-K |
| `jev` | TypeSafe **Score** (bridge relevance) + **Choice** over candidate ids + `SCROLL_DOWN`; finalist Choice among top-K |
| `laya` | Local **Laya** Score + Choice (same semantics as jev; `USE_TF=0`, English checkpoint) |
| `gpt` / `deepseek` / `claude` | JSON action from LLM system prompt (scroll-down + finalist instructions) |

Jev prompts emphasize:

- Score = bridge relevance toward the goal.
- Scroll-down and click each cost one step; minimize total actions.
- Finalist: “you have scrolled N times; pick the best remaining candidate.”

## Metrics

Episode output:

- `status`, `path`, `steps`, `clicks`, `scrolls`, `seconds`, `reason`
- `chosen_score`, `max_score`, `avg_score`, `avg_clicked_score`, `finalist_picks`, `finalist_mode_fired`

**Score honesty:** `avg_score` is the mean over **all scored titles** this episode (`avg_score_scope=all_scored_titles`), not a path-quality metric. Use `avg_clicked_score` (mean bridge score of links actually clicked) when judging path quality.

**Finalist ranking (each step):** observe → `brain.score_only` → `env.ingest_scores` → `env.refresh_finalists` → `brain.choose_action`. Top-K is over **all** scored links on the **current page** after excluding visited/blocked titles (never by recycled link ids). No whole-episode timeout teleport.


## Install

```bash
cd wikirace-bench
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill TYPESAFE_API_KEY for jev; overlap needs none
# for --brain laya: pip install laya  (and USE_TF=0)
```

Needs Chromium/Chrome. Headless default; use `--headed` to watch. CI / sandbox usually needs `--no-sandbox` (already set in `WikiBrowser`).

## Quick start

```bash
# Overlap heuristic
PYTHONPATH=. python run.py play \
  --brain overlap --source browser --lang en \
  --start Coffee --goal Caffeine

# Jev (TypeSafe Score + Choice, finalist at bottom)
PYTHONPATH=. python run.py play \
  --brain jev --source browser \
  --start "Rubber duck" --goal Bathing --timeout 180

# Laya (local Score + Choice; needs `pip install laya`, USE_TF=0)
USE_TF=0 PYTHONPATH=. python run.py play \
  --brain laya --source browser \
  --start "Rubber duck" --goal Bathing --timeout 300

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
    state.py     # RaceState / Action / Candidate / LinkScore
    env.py       # RaceEnv (scores, finalist mode, recovery scrolls)
    brains.py    # Overlap + Jev/Laya (Score/Choice) + LLM brains
    wiki.py
    eval.py
```

## Design notes

- **Stable link ids** within a page visit (href → `L001`…); reused across scrolls so memory ids stay valid.
- **Score book**: best score per title on the current page (cleared on navigation) + episode-wide max for metrics.
- Fixture / live MediaWiki API modes still expose a full-page link list with no real scroll (`can_scroll_down=false`; offline mode does not force finalist_mode — click among full candidates).
- Baidu Baike remains a TODO stub.
