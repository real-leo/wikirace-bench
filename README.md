# WikiRace Bench (whole-page links + click choice)

Wikipedia link-navigation evaluation. The default browser mode collects all
**rendered article links on the current page**, then asks the model to choose
one link to click. It does not scroll through viewports or make a separate
model decision for each screen.

## Default observation: `--observation page`

Each page visit collects its links once from the rendered article DOM:

- Include off-screen article links and rendered infobox, table and navigation-box links.
- Exclude hidden elements, other origins, non-article namespaces, red/edit links and same-page fragments.
- Deduplicate by destination URL; filter previously visited or blocked destinations.
- Preserve each candidate's stable id, article title, actual URL and short local context.
- Supply the goal title and intro, current title and intro, and the last six visited titles.

This does not expand collapsed sections, load linked articles in advance or
provide full article text. “Whole-page” means rendered links in the article
container, including those below the current viewport. No physical scroll to
the bottom is necessary.

## Jev selection policy

| Eligible links | Model calls | Offered click choices |
|---|---|---|
| 1–255 | One Choice call | Every eligible link |
| More than 255 | Score every eligible link in batches of at most 64 questions, then one Choice call | Highest-scoring 255 links |
| 0 | End with `page_links_empty` | None |

Large pages are **not truncated before scoring**. Every eligible link must
receive a valid score before ranking; missing answers fail explicitly. An exact
goal link, if present, is retained in the shortlist. The model still chooses the
click. Batches run sequentially and the episode deadline applies throughout.

The `page-target-v2-255` policy focuses on the identity of the destination: its
location, organization, event series or other distinguishing facts. A shared
word such as “mountain” is weak evidence by itself. Specific relevant lists and
concrete connections receive stronger scores; broad hubs are allowed when they
provide a credible route. There is no instruction to click early or scroll.

Scores use five levels (0–4, normalized to 0–1): unrelated/conflicting entity;
generic topic overlap; plausible broad hub; specific bridge; exact target or
closely connected index. They are ranking hints, not success probabilities.

The two-stage design follows TypeSafe's [public Wikiracing description](https://typesafe.ai/blog/introducing-system-one-models-and-jev):
score large candidate sets, then use Choice within its 255-option limit.
The official demo's complete prompt, DOM filters and shortlist size have not
been published in the sources reviewed. This repository's prompt, 255-link
shortlist, context format and visited-page filtering are our implementation;
this is not an exact reproduction of the demo.

## Actions and metrics

In page mode the only model action is `click` with an offered `link_id`.
Off-screen anchors can be clicked directly without recovery scrolling.
`steps == clicks`, `scrolls == 0`, and page-bottom finalist mode is unused.
Scoring calls consume time and tokens, but are not graph hops.

Episodes stop on reaching the goal, illegal actions, errors or the wall-clock
limit (`--timeout 600` by default). `--max-steps 0` means unlimited; a positive
value remains soft observation information rather than a termination rule.
The action-loop timeout starts after browser and goal setup; outputs separately
report `seconds`, `setup_seconds` and `total_seconds`.

Results record the mode, path, clicks, elapsed time, model version and code
fingerprint. Each trace includes `n_page_links`, `n_eligible_links`,
`n_choice_candidates`, offered URLs, Score/Choice payloads and responses,
`prompt_version`, phase timing and returned API token usage. Completed scoring
batches remain logged if a later API call fails or times out.

`avg_score` averages all scored titles, not route quality. `avg_clicked_score`
averages clicked links with scores. Direct Choice on a small page has no Score
call, so its chosen score can be null. `finalist_picks` refers only to legacy
viewport mode, not the page-mode shortlist.

## Legacy viewport mode

Use `--observation viewport` to reproduce the earlier scroll/click task:

- Observe current-screen article links plus the previous screen's memory.
- Score links, then choose click or `SCROLL_DOWN`; no model `SCROLL_UP`.
- At the page bottom, choose among the current page's five highest-scored links.
- Off-screen memory clicks may require executor recovery scrolls.
- Each click, scroll, recovery scroll and optional translation costs one step.

The previous viewport prompt and narrower link filters are preserved. Page and
viewport mode expose different information and action spaces; compare success,
clicks, time and API usage with the mode clearly stated. Their total step counts
are not an equivalent benchmark of model ability.

## Brains

| Name | Supported browser modes |
|---|---|
| `jev` | Page (default): Score when needed, then Choice; legacy viewport |
| `overlap` | Page and viewport, using token-overlap heuristics |
| `laya` | Page (default) and viewport; local Score + Choice, requires `laya` and `USE_TF=0` |
| `laya-mlx` | Page only; Apple GPU, full candidate coverage with token-checked grouped Choice |
| `semif` | Page only; native Qwen3.5 MLX option scoring, all links in groups of at most 16 |
| `gpt` / `deepseek` / `claude` | Viewport only; JSON actions |

Pass `--observation viewport` explicitly for brains without a page policy (gpt/deepseek/claude).
Fixture and MediaWiki API sources retain their existing non-browser behavior.

Apple Silicon deployment: see the [Laya MLX validation and input audit](reports/2026-09-21-mac-mlx-analysis.md).
The [255-candidate retest and SemIf deployment report](reports/2026-09-21-page-255-retest.md)
contains the new browser results, pinned model revisions and local reproduction commands.
Use `--brain laya-mlx` for the new adapter. It presents the complete title of each
Score candidate, and compares all offered candidates in groups that fit the
checkpoint, repeating with the group winners. Every request is checked against
the tokenizer's full untruncated input. The policy accepts 255 candidates, but
this tournament is not equivalent to Jev's native 255-way Choice. Optional
descriptions are explicitly shortened; full titles are preserved. The old
`--brain laya` CPU adapter remains available for historical reproduction and
still has its 64-option cap and long-input limitations.

```bash
uv venv --python 3.12 .venv-mlx  # once, when this environment does not exist
uv pip sync --python .venv-mlx/bin/python requirements-mlx.lock.txt
HF_HUB_OFFLINE=1 .venv-mlx/bin/python run.py play --brain laya-mlx \
  --source browser --observation page --start Coffee --goal Caffeine --timeout 600
```

The September 20 reports used a 64-link shortlist for large pages. New 255-link
runs have a new policy version and must not be pooled with those older runs.

The [all-link, extended-budget comparison](reports/2026-09-21-grouped-long-comparison.md)
also tests `--page-policy grouped-all` through `scripts/run_mlx_retest.py`.
This sends every eligible link through a tournament before clicking, with no
Score-to-255 prefilter. Laya groups contain at most 8 links within 512 tokens;
SemIf groups contain at most 16 links within a 4,096-token application budget.
Groups shrink if their complete inputs do not fit. Group probabilities are
conditional on their own options, so winners are compared again in later rounds.
SemIf requires `.venv-semif`, the local source checkout and pinned model described
in the deployment report; it defaults to this all-link policy.
Create the pinned checkout before installing the SemIf lockfile (run from the
repository root; model download details are in the deployment report):

```bash
git clone https://github.com/TheoLeeCJ/SemIf.git third_party/SemIf
git -C third_party/SemIf checkout ca3ba65f142967030ecb453346e94d6f476a69df
uv venv --python 3.12 .venv-semif
uv pip sync --python .venv-semif/bin/python requirements-semif.lock.txt
```

The completed three-task run (up to 60 minutes per task) reached 3/3 goals with
Jev, 2/3 with SemIf, and 2/3 with Laya MLX. Laya's Music task finished at about
32 minutes after missing the 30-minute checkpoint. These are single-run system
results on live pages, not a fixed-input model ranking; see the report for
candidate differences, input audits and the excluded red-link failure run.

An [offline 255-option capacity experiment](reports/2026-09-21-wide-choice-analysis.md)
also verifies in-memory context/answer-code extensions for both local backends.
Both execute a single 255-option decision, but Laya's simple target checks remain
weak and SemIf's measured speedup is small. This exploratory script does not
change the default adapters or model files.

For a matched-input comparison, use the new
[unified protocol](reports/2026-09-21-unified-protocol.md) and
`scripts/run_unified_comparison.py --out-root runs/unified-255/new-run --timeout 3600`.
All three backends receive the same rules, goal/current descriptions, recent path,
full candidate titles and short contexts. All cover the eligible links in groups
of up to 255, without a backend-specific Score prefilter. Both local tokenizers
jointly determine group sizes, and fixed-input hashes must match before live races.
This is a separate protocol; use its dedicated runner rather than the legacy defaults.
The [matched-input results](reports/2026-09-21-unified-comparison.md) distinguish
completed fixed-input checks from completed or pending full races.

The [Gemini chat baseline](reports/2026-09-21-gemini-unified-comparison.md) uses
the same evidence, rule text, 255-option groups and tournament. Set
`HOTDAY_API_KEY`, `HOTDAY_BASE_URL` and `HOTDAY_MODEL` in the ignored `.env`.
It requests strict JSON choices and rejects invalid or truncated responses.
The runner audits fixed-input parity first, then starts the three browser tasks.
Cloud API inference can run alongside the local GPU benchmark; the report
records that overlap. Add `--wait-for-baseline` only to request serial execution:

```bash
.venv-semif/bin/python scripts/run_gemini_comparison.py \
  --out-root runs/gemini-unified/new-run \
  --baseline-root runs/unified-255/2026-09-21-v3 --timeout 3600
```

Actual chat request bodies, response model names and usage are saved without
authorization headers. The add-on adapter lives under `scripts/` and records
its source hash alongside the shared runtime fingerprint.

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

### Local setup with uv (tested on macOS)

```bash
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python requirements.lock.txt
cp .env.example .env   # only for a new checkout; preserve an existing .env
```

Set `TYPESAFE_API_KEY` in `.env`. macOS Chrome and Linux Chrome/Chromium are
detected automatically. Set `WIKIRACE_BROWSER_PATH` for another executable.
If this network requires a proxy, set `WIKIRACE_BROWSER_PROXY` explicitly;
Chromium does not inherit the shell's `HTTPS_PROXY`. Do not put credentials in
the browser proxy URL. `.env`, `.venv/`, and `runs/` are ignored by Git.

```bash
.venv/bin/python -m pytest -q tests
.venv/bin/python run.py play --brain jev --source browser \
  --start Coffee --goal Caffeine --timeout 45 --out runs/coffee-caffeine.json
.venv/bin/python run.py bench --brains jev --tasks data/tasks_hard.json \
  --timeout 600 --out runs/hard.jsonl
```

`play --out` preserves the complete episode. Each step records the observation,
offered candidates, scores, Score/Choice requests and responses (without auth
headers), phase timings, token usage, and execution result. Episodes include a
run id, UTC start time, code fingerprint, configured/resolved model versions,
and goal description. `seconds` retains the action-loop timing used by earlier
runs; `setup_seconds` and `total_seconds` also expose browser/goal preparation.
The timeout prevents further decisions or actions after expiry, but an
in-flight request/navigation may finish after the deadline.

Navigation waits for a **new document** with a parsed Wikipedia article and
heading, stable for 0.5 seconds. Browser scripts use the current JavaScript
context so an old document handle cannot masquerade as a loaded page.
The per-attempt navigation limit is 30 seconds, capped by the remaining episode
budget. One reload of the same selected URL is allowed for a navigation timeout,
selected transient network errors, or HTTP 500/502/503/504; HTTP 403/404/429 are
reported immediately. Reloads consume elapsed time and appear in
`trace[].navigation.attempts`; they do not add a model decision or graph hop.
The trace includes URL, HTTP status (when Chromium exposes it), document state,
browser error code, and a short error-page excerpt. Setup navigation is saved
separately. No whole-page article text is added to the model observation.

Network/browser failures are `error` results with a saved traceback, rather
than empty finalist losses. A failed episode does not abort the batch. Policy
score memory resets between tasks, including consecutive tasks with the same
starting page. The tests include one live MediaWiki intro smoke check; other
regressions use deterministic transports and do not call Jev.

Jev reuses its HTTP connection pool within an episode. Transient transport
errors, rate limits and temporary gateway/overload responses are retried at
most twice, with backoff and the remaining episode time as a limit. Invalid
credentials and request-validation errors are not retried. `api_usage` reports
usage returned by completed API responses; usage for requests that never
returned a response is unknown. Retries can consume additional API tokens.

TypeSafe documentation snapshots and source links are in `docs/reference/`.
They are reference documents only; no agent skill or plugin is installed.

## Quick start

```bash
# Overlap heuristic
PYTHONPATH=. python run.py play \
  --brain overlap --source browser --lang en \
  --start Coffee --goal Caffeine

# Jev (whole-page candidates; Score when needed, then Choice)
PYTHONPATH=. python run.py play \
  --brain jev --source browser \
  --start "Rubber duck" --goal Bathing --timeout 180

# Laya (local Score + Choice; needs `pip install laya`, USE_TF=0)
USE_TF=0 PYTHONPATH=. python run.py play \
  --brain laya --source browser --observation viewport \
  --start "Rubber duck" --goal Bathing --timeout 300

# Harder race (10-minute wall-clock safety timeout)
PYTHONPATH=. python run.py play \
  --brain jev --source browser \
  --start Tea --goal Moon --timeout 600
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
  --observation page|viewport  # default: page (browser)
  --timeout 600
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
    browser.py   # DrissionPage + page/viewport links + local context
    state.py     # RaceState / Action / Candidate / LinkScore
    env.py       # RaceEnv (page shortlist + legacy viewport actions)
    brains.py    # Overlap + Jev/Laya (Score/Choice) + LLM brains
    page_policy.py # Jev whole-page Score/Choice prompt
    wiki.py
    eval.py
```

## Design notes

- **Stable link ids** within a page visit (href → `L001`…); reused across scrolls so memory ids stay valid.
- **Score book**: best score per title on the current page (cleared on navigation) + episode-wide max for metrics.
- Fixture / live MediaWiki API modes still expose a full-page link list with no real scroll (`can_scroll_down=false`; offline mode does not force finalist_mode — click among full candidates).
- Baidu Baike remains a TODO stub.
