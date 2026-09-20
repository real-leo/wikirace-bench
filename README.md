# WikiRace Bench（DrissionPage 视口版）

基于 **真实浏览器视口** 的 Wikipedia WikiRace 评测台。每一步大脑只看到当前浏览器 **VIEWPORT 内可见** 的正文内链，而不是整页 MediaWiki `prop=links` 全量列表。

## 核心流程

1. 用 DrissionPage 打开 `https://{lang}.wikipedia.org/wiki/{title}`
2. 用 JS（`getBoundingClientRect` vs viewport）在 `#mw-content-text` / `#bodyContent` 内抽取**视口可见**文章链接
3. 大脑根据 `RaceState` 返回一个 JSON action：`click` / `scroll` / `translate`
4. 非法 `link_id` → 本局失败；`scroll` / `translate` 也消耗一步
5. 到达目标页（规范化标题匹配）→ success

**主模式：`--source browser`（默认）**。`fixture` 离线图与 `live` MediaWiki API 仍可用作对照，但新设计以浏览器视口为准。

## 依赖

- Python 3.10+
- **Chromium / Chrome**（系统已安装即可；DrissionPage 会驱动它）
- Python 包见 `requirements.txt`（含 `DrissionPage`）

### 安装

```bash
cd wikirace-bench
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 接 LLM 时填 key；overlap 启发式不需要
```

Headless 默认开启。无 GUI 调试加 `--headed`。无沙箱/CI 环境通常需要 Chrome 参数 `--no-sandbox`（本仓库浏览器模块已设置）。

## 快速开始

### 浏览器短局（overlap 启发式）

```bash
PYTHONPATH=. python run.py play \
  --brain overlap --source browser --lang en \
  --start Coffee --goal Caffeine --max-steps 8
```

有界面：

```bash
PYTHONPATH=. python run.py play --brain overlap --source browser --headed \
  --start "Rubber duck" --goal Bathing --max-steps 10
```

### 离线 fixture（不启浏览器）

```bash
PYTHONPATH=. python run.py play --brain overlap --source fixture \
  --start Coffee --goal "Apollo 11"
```

### 批量 bench

```bash
# 跑 tasks.json（含 fixture + browser 任务）
PYTHONPATH=. python run.py bench --brains overlap

# 全部强制浏览器源
PYTHONPATH=. python run.py bench --brains overlap --source browser --lang en
```

### 接真模型

```bash
PYTHONPATH=. python run.py bench --brains overlap,jev,claude,gpt,deepseek --source browser
```

缺哪个 key 就从 `--brains` 去掉哪个。模型名见 `.env.example`。

## 观察 RaceState

```json
{
  "goal": {"title": "Caffeine", "extract": "..."},
  "current": {"title": "Coffee", "extract": "A brewed drink..."},
  "history": ["Coffee"],
  "candidates": [
    {"id": "L001", "title": "Caffeine", "href": "https://en.wikipedia.org/wiki/Caffeine", "text": "caffeine"},
    {"id": "L002", "title": "Ethiopia", "href": "...", "text": "Ethiopia"}
  ],
  "step": 0,
  "max_steps": 8,
  "source": "browser"
}
```

- 候选链接**每步重新编号**为 `L001`, `L002`, …
- `browser` 模式下只有视口内可见链接；滚动后集合会变
- `fixture` / `live` 模式下仍是整页链接表（无真实视口）

## Action schema

大脑必须返回以下之一：

```json
{"action": "click", "link_id": "L001"}
{"action": "scroll", "direction": "down", "amount": "page"}
{"action": "scroll", "direction": "up", "amount": "half"}
{"action": "translate", "target_lang": "zh"}
```

| 动作 | 效果 | 耗步 |
|------|------|------|
| `click` | 点击视口候选；非法 id → fail | 是 |
| `scroll` | 上/下滚一页或半页，刷新可见链接 | 是 |
| `translate` | 用 Google Translate 网页包装当前 URL（`translate.google.com/translate?sl=auto&tl={lang}&u={url}`） | 是 |

**翻译策略说明**：选用 Google Translate website wrapper，而不是 Wikipedia 语言版跳转——这样仍是同一篇文章的内容翻译，不切换语言版的链接图。若需改成「点语言链接换 `zh.wikipedia.org`」，可在 `WikiBrowser.translate` 替换实现。

目标匹配：对标题做空格/下划线与空白规范化后比较（`normalize_wiki_title`）。

## Brains

| 名称 | 说明 |
|------|------|
| `overlap` | 启发式：优先点与 goal 词重叠高的链接；重叠过低则 `scroll down` |
| `gpt` / `deepseek` | OpenAI 兼容 Chat Completions，返回新 action JSON |
| `claude` | Anthropic Messages API |
| `jev` | Typesafe Choice（候选 + 可选 SCROLL_DOWN） |

## CLI 要点

```
run.py play|bench
  --source browser|fixture|live
  --lang en
  --headless / --headed     # 默认 headless=true
  --brain / --brains
  --start --goal --max-steps
```

## 百度百科

`BaiduBaikeSource` 仅为 **TODO stub**（`source=baidu` 会 `NotImplementedError`）。接口预留与 Wikipedia 相同的 action schema。

## 目录

```
wikirace-bench/
  run.py
  requirements.txt
  .env.example
  data/tasks.json
  data/fixture_wiki.json
  wikirace/
    browser.py   # DrissionPage + 视口链接 JS
    state.py     # RaceState + Action
    env.py       # RaceEnv
    brains.py
    wiki.py      # fixture / live API / baidu stub
    eval.py
```

## 限制与排障

- 需要本机可启动的 Chrome/Chromium。若 DrissionPage 找不到浏览器，请安装 Google Chrome 或设置浏览器路径。
- Headless 在部分环境需 `--no-sandbox`（已默认加上）。
- Google Translate 包装页 DOM 与原版维基略有差异，视口链接抽取仍尽量走 `#mw-content-text`。
- `live`（MediaWiki API）不是视口模式，仅作遗留对照；公平评测请用 `browser`。
