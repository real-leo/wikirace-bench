# Jev 本地验证记录 — 2026-09-20

项目已连接到 `https://github.com/real-leo/wikirace-bench.git`，`main` 跟踪 `origin/main`。基线提交为 `f0e5210cd90cbedfdcd1010379121a9a640e21dd`。同步前的文件完整备份在 `/Volumes/MachineU1/github_work/wikirace-bench.backup-20260920-194607`。

## 环境与修改

- 使用 uv 创建 Python 3.12.13 环境，依赖版本保存于 `requirements.lock.txt`。
- 配置了 `.env`，权限为 0600，Git 忽略；报告和运行日志不包含密钥。
- TypeSafe 文档仅保存在 `docs/reference/`，未安装 skill 或插件。
- 远端提交已经修复 finalist 时序、完整视口评分、先过滤再取 top-K、跨页编号黑名单和严格动作校验，并提供目标简介。
- 本地补充 macOS 浏览器查找、显式代理配置、错误页检测、失败后的资源释放、跨任务评分记忆清空、分阶段日志和完整 JSON 输出。
- 根据第一轮 TLS 中断，增加 Jev 连接复用与有限重试。网络/过载错误最多重试两次，认证错误不重试，重试受剩余时间限制。
- 21 项测试通过，包括真实 MediaWiki 目标简介烟测；其余测试使用受控传输，不消耗 Jev API。

## 真实 Jev 页底对照

这是相同受控视口下的 API 实验，不是真实浏览器导航成绩。初始 finalist 只有 NASA，最后一屏新出现目标 Moon。

| 代码 | Moon 新评分 | 实际 Choice 候选 | Jev 选择 | 到达目标 |
|---|---:|---|---|---|
| 旧版 90aeb3f | 0.9950 | NASA | NASA | 否 |
| 新版 f0e5210 + 本地运行修复 | 0.9925 | Moon、NASA | Moon | 是 |

证据：`runs/finalists-before.json`、`runs/finalists-after.json`。这验证了候选生成修复的实际效果，不能单独证明困难任务成功率提高。

## 烟测

| 任务 | 来源 | 结果 | 点击 / 滚动 | 动作耗时 |
|---|---|---|---|---:|
| Coffee → Apollo 11 | fixture + Jev | 成功 | 7 / 0 | 34.628 秒 |
| Coffee → Caffeine | 真实浏览器 + Jev（连接复用后） | 成功 | 1 / 0 | 7.299 秒 |

浏览器烟测另有 41.188 秒的环境准备，总耗时 48.487 秒。成功局路径和请求证据分别保存在 `runs/jev-fixture-coffee-apollo.json`、`runs/jev-browser-coffee-caffeine.json`。

## 两轮困难任务

两轮均已包含远端候选修复和真实目标简介；第二轮额外启用连接复用与有限重试。每轮三局并发，动作阶段上限均为 300 秒。滚动、点击及恢复滚动计步，评分和 Choice 请求消耗时间及 token，但不算浏览器动作。

`seconds` 为动作阶段时间；准备时间单独列出。网络请求可能在时间上限后才返回，代码不会在发现超时后继续开始下一项动作。不能将本机耗时与此前其他机器的聊天汇总直接比较。

| 轮次 | 任务 | 结果 | 总步数 | 点击 / 滚动 | 动作耗时 | 准备耗时 | 页底选择 |
|---|---|---|---:|---|---:|---:|---:|
| round1 | DNA → Manipuri pony | 环境/API 错误 | 24 | 22 / 2 | 254.7s | 54.5s | 0 |
| round1 | Music → 2001 AAA Championships | 超时 | 37 | 22 / 15 | 300.5s | 54.9s | 0 |
| round1 | World War II → Bald Mountain Recreation Area | 超时 | 31 | 20 / 11 | 300.2s | 54.1s | 0 |
| round2 | DNA → Manipuri pony | 成功 | 62 | 49 / 13 | 285.4s | 54.6s | 0 |
| round2 | Music → 2001 AAA Championships | 超时 | 53 | 36 / 17 | 306.8s | 55.7s | 0 |
| round2 | World War II → Bald Mountain Recreation Area | 环境/API 错误 | 59 | 40 / 19 | 278.2s | 55.6s | 1 |

第二轮结果的解释：

- **DNA 成功**：末段经 `List of horse breeds → Burmese Horse → Shan Horse → Manipuri pony` 到达目标。共 49 次点击、13 次滚动，没有触发页底 finalist。
- **Music 超时**：到达田径和英国相关页面后仍绕路，最后停在 `1983 World Championships in Athletics`，没有到达目标赛事。
- **WWII 属于环境错误**：已进入 `Michigan → Southeast Michigan → Oakland County, Michigan`，后续选择 `Pontiac, Michigan` 时未能加载文章正文，错误为 `wikipedia_page_unavailable`。该局不能作为一条干净的导航失败来分析。
- 第一轮 DNA 在 Score 阶段发生 TLS EOF；第二轮未发生 Jev API 传输错误，完成了目标。第二轮 API 重试计数均为零；重试分支的有效性通过模拟故障测试验证。
- 第二轮并未改变导航提示词，所以不能把差异解释成模型决策能力提高。任务样本少、页面和网络动态变化，尚未证明困难任务成功率或路径长度有稳定改善。

## 耗时与调用量

下表将成功返回的 Score/Choice 阶段耗时除以已记录响应数。失败请求若未返回响应，其 token 用量未知。

| 轮次 | 任务 | API 响应数 | Score+Choice 耗时 | 每响应平均耗时 | 输入 token | 输出 token |
|---|---|---:|---:|---:|---:|---:|
| round1 | DNA → Manipuri pony | 58 | 153.6s | 2.65s | 203,942 | 12,399 |
| round1 | Music → 2001 AAA Championships | 76 | 191.2s | 2.52s | 266,133 | 15,923 |
| round1 | World War II → Bald Mountain Recreation Area | 75 | 198.4s | 2.64s | 286,066 | 17,397 |
| round2 | DNA → Manipuri pony | 158 | 118.1s | 0.75s | 660,547 | 39,963 |
| round2 | Music → 2001 AAA Championships | 124 | 104.7s | 0.84s | 464,381 | 27,905 |
| round2 | World War II → Bald Mountain Recreation Area | 131 | 97.8s | 0.75s | 486,340 | 28,875 |

第二轮 API 平均耗时低于第一轮，但不同页面、网络波动和并发负载仍是混杂因素。浏览器执行与页面加载成为更显著的剩余开销。每局的环境准备约 54–56 秒不包含在 300 秒动作预算中，报告已分别保留，不能把动作耗时当作端到端耗时。

## 已确认的机制修复与后续策略问题

1. 页底新出现的高分候选会在 Choice 前进入最新 finalist；真实 Jev 受控对照已验证。
2. Jev 分批覆盖所有可见链接；测试验证第 25 个目标仍被评分。
3. 访问记录先过滤、再取 top-K；合法的第六名不会因为前五名已访问而消失。
4. 防循环按页面标题处理，不再把其他页面复用的 L001 一起拉黑。
5. 已知但不在 offered 集合中的 memory 编号也会被拒绝。
6. 浏览器网络错误与 API 错误会单独记录，异常堆栈、候选、评分、最终动作均可复核。

WWII 第一轮的目标简介已包含 Michigan、Lake Orion，但模型早早从 U.S. state 跳向州政府、行政区划等通用概念，没有先观察到 Michigan 候选。下一步应通过有对照的实验调整“当前桥梁是否足够好、是否值得再滚一屏”的策略，不能单靠扩大 top-K、延长超时或从整局旧链接中直接跳转来证明决策改善。

## 文件与复现

- [round1 / DNA → Manipuri pony](/Volumes/MachineU1/github_work/wikirace-bench/runs/round1/jev-hard-dna.json)
- [round1 / Music → 2001 AAA Championships](/Volumes/MachineU1/github_work/wikirace-bench/runs/round1/jev-hard-music.json)
- [round1 / World War II → Bald Mountain Recreation Area](/Volumes/MachineU1/github_work/wikirace-bench/runs/round1/jev-hard-wwii.json)
- [round2 / DNA → Manipuri pony](/Volumes/MachineU1/github_work/wikirace-bench/runs/round2/jev-hard-dna.json)
- [round2 / Music → 2001 AAA Championships](/Volumes/MachineU1/github_work/wikirace-bench/runs/round2/jev-hard-music.json)
- [round2 / World War II → Bald Mountain Recreation Area](/Volumes/MachineU1/github_work/wikirace-bench/runs/round2/jev-hard-wwii.json)
- [受控对照：旧版](/Volumes/MachineU1/github_work/wikirace-bench/runs/finalists-before.json)
- [受控对照：新版](/Volumes/MachineU1/github_work/wikirace-bench/runs/finalists-after.json)
- [汇总 JSON](/Volumes/MachineU1/github_work/wikirace-bench/reports/2026-09-20-jev-summary.json)

```bash
uv pip sync --python .venv/bin/python requirements.lock.txt
.venv/bin/python -m pytest -q tests
.venv/bin/python run.py bench --brains jev --tasks data/tasks_hard.json \
  --timeout 300 --out runs/hard-new.jsonl
```

上述 bench 命令按任务顺序执行；本报告两轮使用三个独立 play 进程并发执行。比较耗时时需保持并发设置一致。任务定义位于 `data/tasks_hard.json`，原始 runs 目录被 Git 忽略；如需共享实验，应单独选择要发布的脱敏结果，勿发布 `.env`。
