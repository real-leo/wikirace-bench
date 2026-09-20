# 整页链接 + 目标身份提示：实跑验证

2026-09-20。按本轮要求，将浏览器默认模式改为整页候选、只点击，并调整提示。两局此前在本机 600 秒超时的任务，本次均成功：Music 6 次点击，WWII 4 次点击。本次每个任务只运行一次，不能据此估计稳定成功率。

## 实现

每次进入新文章，从已渲染的文章容器一次收集 A 标签的 URL、标题和短上下文。包含当前屏幕之外的链接及已展开的表格、信息框和导航框；过滤隐藏元素、外部站点、非文章命名空间、当前页片段和重复 URL。不会实际滚到底，也不预取候选文章正文。折叠区域不自动展开。

先过滤已访问目标。剩余候选不超过 255 条时直接 Choice；超过时，每批至多 64 个 Score 问题，覆盖全部候选，再取前 64 条交给 Choice。评分缺失会明确报错，不会静默当成零分。若存在目标文章链接，保证它进入候选短名单，最终仍由模型选择点击。

`page-target-v1` 提示使用目标简介中的具体地点、组织、赛事系列等身份信息，区分主题相似与有效路线。去掉原视口提示“合理的概念桥梁就够了、尽早点”的偏好；保留可信的宽泛中间节点，避免只看标题字面匹配。请求包含实际 URL、至多 160 字符的链接上下文、当前页和目标简介，以及最近六个页面。

入口默认 `--observation page`，目前支持 Jev / Overlap。旧规则保留为 `--observation viewport`。整页模式的步数等于点击数，滚动为零；Score / Choice 调用计耗时和 API 用量，不计图上的跳转数。默认动作阶段超时仍是 600 秒。

实现：[page_policy.py](/Volumes/MachineU1/github_work/wikirace-bench/wikirace/page_policy.py)、[env.py](/Volumes/MachineU1/github_work/wikirace-bench/wikirace/env.py)、[browser.py](/Volumes/MachineU1/github_work/wikirace-bench/wikirace/browser.py)。

## 数据

模型均为 API 返回的 `jev-1.13.0`。本次两个难任务和一个直接目标检查并行运行，浏览器与网络耗时可能受并发影响。动作阶段计时与旧版一致，从环境准备完成后开始；另列端到端时间，避免把启动开销漏掉。

| 任务 / 模式 | 结果 | 点击 | 滚动 | 动作阶段 | 含准备总耗时 |
|---|---|---:|---:|---:|---:|
| Music → 2001 AAA，旧视口 | 600 秒超时 | 71 | 75 | 600.124 秒 | 640.672 秒 |
| Music → 2001 AAA，新整页 | 成功 | 6 | 0 | 129.529 秒 | 185.500 秒 |
| WWII → Bald Mountain，旧视口 | 600 秒超时 | 94 | 32 | 600.120 秒 | 639.760 秒 |
| WWII → Bald Mountain，新整页 | 成功 | 4 | 0 | 152.224 秒 | 208.548 秒 |
| U.S. state → Michigan，新整页检查 | 成功 | 1 | 0 | 32.245 秒 | 83.550 秒 |

路径均来自执行后的真实页面标题：

- Music → Performing arts → Entertainment → Sport → Sport of athletics → AAA Championships → 2001 AAA Championships。
- World War II → United States → Michigan → List of Michigan state parks → Bald Mountain Recreation Area。

这次同时改变了信息覆盖、链接过滤范围、评分/选择方式和提示；它说明这一组合在两局上有效，不能把改善全部归因于提示，也不能用总步数直接衡量原视口规则下的能力提升。没有证明路线是最短路径。

## 大候选的覆盖与代价

真实 U.S. state 页面在滚动位置 0 时，视口模式提取 18 条，整页模式提取 440 条。Michigan 不在视口候选中，但在整页候选中；实跑一跳到达。另用本地 DOM 页面验证了屏幕外链接、信息框/导航框、重复链接、隐藏元素、透明元素、外站链接和 File 命名空间的处理。证据：[page-links-smoke.json](/Volumes/MachineU1/github_work/wikirace-bench/runs/page-links-smoke.json)。

WWII 的四次决策分别面对 1,149 / 1,835 / 1,033 / 304 条页面链接，过滤后的 1,148 / 1,833 / 1,030 / 302 条全部被评分，没有先截前 255 条。每次随后从 64 条中做 Choice。Music 有两页候选不超过 255，直接 Choice；其余页面评分后选择。

| 本次运行 | 完成的 API 请求 | API 输入 tokens | API 输出 tokens | Score 耗时 | Choice 耗时 | 点击执行耗时 |
|---|---:|---:|---:|---:|---:|---:|
| Music | 43 | 840,893 | 45,423 | 65.127 秒 | 8.472 秒 | 52.756 秒 |
| WWII | 73 | 1,570,495 | 77,290 | 99.334 秒 | 2.505 秒 | 47.992 秒 |

整页策略减少了绕路和动作，但不保证减少 tokens。WWII 的旧视口局输入为 1,101,499 tokens，本次反而约为其 1.43 倍。这里计的是 API 返回的 usage，不等同于一个普通文本模型上下文窗口中塞了这么多 tokens，也不是价格估算。当前主要时间花在大页面评分和真实网页跳转；把点击减少到四次，不等于只调用模型四次。

## 页面加载与回归核对

本次三次运行共 11 次点击全部完成，最终导航状态均为 HTTP 200，无加载重试或加载错误。每次检查新文档的标识、文章根节点和标题，继续使用上一轮修复后的加载逻辑。这是本轮未复现错误的证据，不保证所有网络条件下都不会再失败。

39 项测试通过，包含 255 条边界、601 条候选完整评分、目标在列表末尾、禁止滚动、拒绝未提供的链接、评分缺失报错，以及中途 API 失败保留已完成批次的用量。另核对真实日志中的所有选择均来自提供的真实链接、候选数未超 Choice 上限、当前代码指纹与运行记录一致，输出中未包含 API key。

运行指纹：`783f5e7acf7cf50433ae894ce6410ac36e8bf96bbce33632cc8ba8a06ed35e38`。

## 与官方 Demo 的关系

采用 [TypeSafe 公开文章](https://typesafe.ai/blog/introducing-system-one-models-and-jev)所述“大候选先评分，再 Choice”的结构。官方原始提示、完整 DOM 过滤方式、短名单大小等在已查公开资料中没有找到。因此这里提供了同类的页级文章候选，但不能声称输入逐项相同或使用了官方的特殊 prompt。调查过程见 [官方 Demo 对照报告](/Volumes/MachineU1/github_work/wikirace-bench/reports/2026-09-20-official-demo-comparison.md)；其中“下一轮”的建议属于本轮用户明确改为整页模式之前的阶段记录。

## 复现与原始记录

```bash
.venv/bin/python run.py play --brain jev --source browser --observation page \
  --start "World War II" --goal "Bald Mountain Recreation Area" \
  --timeout 600 --out runs/wwii-page.json

.venv/bin/python -m pytest -q tests
```

- [Music 完整日志](/Volumes/MachineU1/github_work/wikirace-bench/runs/round4-page/jev-hard-music.json)
- [WWII 完整日志](/Volumes/MachineU1/github_work/wikirace-bench/runs/round4-page/jev-hard-wwii.json)
- [Michigan 检查日志](/Volumes/MachineU1/github_work/wikirace-bench/runs/page-michigan-smoke.json)
- [本轮精简数据](/Volumes/MachineU1/github_work/wikirace-bench/reports/2026-09-20-page-mode-summary.json)

代码和报告保存在当前仓库，未提交或推送远端。
