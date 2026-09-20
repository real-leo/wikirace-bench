# 页面加载调查与 600 秒复跑

## 已确认的原因与证据边界

上一轮 WWII 在 `Southeast Michigan → Pontiac, Michigan` 的点击执行阶段退出，动作耗时 278.219 秒，尚未到 300 秒上限。模型选择的是当前视口中的合法链接 L009，错误为 `wikipedia_page_unavailable`。这不是非法动作，也不是比赛超时。

旧日志没有 HTTP 状态、浏览器错误码或失败时的正文快照，因此无法事后唯一确定那一次失败是否还叠加了网络故障。

真实浏览器复现确认了加载等待的竞态：

1. `a.click()` 发起异步导航，紧接着的 `doc_loaded()` 可能仍看到旧文档已经加载完毕。
2. 原来的正文检查不验证文档是否已经切换。旧文档的正文存在，也可能满足检查。
3. DrissionPage 默认通过缓存的文档对象执行 JS。导航切换时该对象可能失效，实际复现了 `ContextLostError`。
4. 等待代码吞掉异常，最后只报统一的“正文不存在”，掩盖了状态；原有正文轮询也只有约 4 秒。

旧版同一跳转的三次结果保存在 [page-load-before.json](/Volumes/MachineU1/github_work/wikirace-bench/runs/page-load-before.json)：

| 次数 | 旧函数返回 | 返回后实际状态 |
|---|---|---|
| 1 | 成功，0.996 秒 | HTTP 200，但 `readyState=loading`，标题为空，正文根节点不存在 |
| 2 | 成功，5.041 秒 | 后续快照读取超时，无法确认就绪 |
| 3 | 成功，0.533 秒 | HTTP 200，已有正文，但仍为 `loading` |

这些数据证明旧版确实会过早报告加载成功，不能据此认定 Pontiac 页面本身失效。另一次初始探测遇到上下文失效，但脚本在保存结果前退出，未计入上述三次表格。

## 修复

- `play`、`bench` 和 Python evaluator 的默认比赛时限统一为 **600 秒**。仍支持显式 `--timeout`；准备时间与比赛动作时间分别统计。
- 所有浏览器 JS 改用当前执行上下文，避免依赖旧文档对象。
- 等待 `performance.timeOrigin` 发生变化，且新文档达到 `interactive/complete`、具有维基正文和文章标题，持续稳定至少 0.5 秒，才开始观察下一页。重定向后的新文档也支持。
- 页面获取交给上述条件判断就绪，不再额外等待全部图片等资源，也不主动停止这些资源的加载。
- 单次导航最多等待 30 秒，并受比赛剩余时间限制。对等待超时、指定瞬时连接错误和 HTTP 500/502/503/504，最多重试一次同一条已选 URL。403、404、429 直接报告。
- 记录 `navigation.attempts`、URL、HTTP 状态、文档状态、错误码和最多 500 字符的错误页摘录。诊断不会增加给模型的页面信息。
- 网络重载属于同一次点击的恢复，不新增模型决策或图跳转，但耗时计入比赛，重试次数单独记录。点击与滚动仍等价计步。

## 验证

31 项测试通过。新回归覆盖：旧页面不能满足新导航、正文延迟超过 4 秒、已有正文但仍在加载、502 仅重载同一 URL、403/404/429 不重试、达到比赛时限不再重试、初始快照失败不能关闭文档身份检查，以及保存异常与诊断信息。

修复等待逻辑后，同一条真实跳转连续三次通过，导航完成时记录到的状态均为 HTTP 200、`complete`、正文和标题存在，之后均读到 18 条视口链接，点击耗时分别为 8.303、4.763、2.840 秒。证据：[page-load-after.json](/Volumes/MachineU1/github_work/wikirace-bench/runs/page-load-after.json)。其中第一次额外的 2 秒快照读取超时；最终配置将常规观察脚本时限设为 10 秒，短周期就绪探测仍会保留异常并继续等待。

上述三次浏览器探测验证的是等待/上下文修复；后续的加载模式设置和最终配置通过下面的完整比赛复跑验证。

## 完整比赛复跑

Music 和 WWII 各使用 600 秒动作预算，从原起点重新开始，使用相同 Jev 配置与原有决策策略。两局并发运行，不注入路径提示。

| 任务 | 结果 | 总步数 | 点击 / 滚动 | 动作耗时 | 准备耗时 | 页底选择 |
|---|---|---:|---|---:|---:|---:|
| Music → 2001 AAA Championships | 超时 | 146 | 71 / 75 | 600.124s | 40.548s | 2 |
| World War II → Bald Mountain Recreation Area | 超时 | 126 | 94 / 32 | 600.120s | 39.640s | 0 |

两局均运行至完整时间预算，没有因页面加载错误提前终止。包括初始页面在内，167 次已完成导航均记录到 HTTP 200、正文和文章标题存在、文档已解析。165 次属于成功完成的点击。两局均没有触发页面重载重试，重试分支由故障注入测试覆盖。

WWII 末尾还有一次未完成的点击：`Leisure → Public parks`。该导航只有约 1.061 秒剩余预算，随后记录 `navigation_timeout`，整局结果为 `reason=timeout`。这是比赛总时限截断，不能当作又一次提前发生的正文加载故障；这次未完成点击没有计入 94 次已完成点击。

### 多出来的 300 秒用在了哪里

- Music 在约 303 秒时仍处于 `Sport in the United Kingdom` 附近，后来进入英国历史、议会、联邦制、欧盟与 Brexit，最后停在 `England and Wales`。页底强制选题触发了两次，也没有把路径带回目标赛事。
- WWII 在约 300 秒时由美国国家公园列表转到 `Protected area`；后续经过生态、地理、宇宙、大峡谷，再到自然保护与休闲概念，最后到 `Leisure`。本轮没有到达 Michigan，页底强制选择也没有触发。
- 两局目标简介分别已有 AAA/Birmingham/England，以及 Lake Orion/Michigan。因此本轮漂移不能归因于没有目标地理简介。当前策略仍会把一般语义相关当作有效桥梁，延长时间没有自动改善路径。

Music 的 Score/Choice 共消耗 260.364 秒，浏览器动作执行 320.612 秒；WWII 的 Score/Choice 为 228.036 秒，已完成动作执行为 362.070 秒（末次截断导航另在诊断中记录）。剩余主要是观察与编排开销。浏览器加载依然有实际耗时，不应通过提前返回“成功”来降低这些数值。

模型实际返回版本均为 `jev-1.13.0`。Music 341 次、WWII 288 次已记录 API 响应；无 API 重试。原始证据与汇总：

- [Music 完整日志](/Volumes/MachineU1/github_work/wikirace-bench/runs/round3-600s/jev-hard-music.json)
- [WWII 完整日志](/Volumes/MachineU1/github_work/wikirace-bench/runs/round3-600s/jev-hard-wwii.json)
- [耗时、导航状态、300 秒检查点与调用量汇总](/Volumes/MachineU1/github_work/wikirace-bench/reports/2026-09-20-page-load-summary.json)

本轮完整比赛的代码指纹为 `28145e0c629e7c8ad97ab14c3a6f4b85a85059928dcdff7459e252e4cc9ac94f`。比赛运行过程中，补充了“初始快照失败必须先确认旧文档身份再点击”的防护及单测；运行中的两局没有热加载此变更，最终版本另用不调用模型的真实导航探测验证，记录保存在 [page-load-final.json](/Volumes/MachineU1/github_work/wikirace-bench/runs/page-load-final.json)。

最终版本探测 **3/3 通过**：每次均验证新旧文档身份不同、HTTP 200、正确文章标题和简介，并读到 18 条视口链接。点击耗时为 14.325、4.485、7.284 秒；均未重试。探测保存的代码指纹已与最终本地代码核对一致。

复现命令：

```bash
.venv/bin/python -m pytest -q tests
.venv/bin/python scripts/probe_navigation.py --out runs/page-load-final.json
.venv/bin/python scripts/run_load_validation.py hard-music \
  --timeout 600 --out runs/round3-600s/jev-hard-music.json
.venv/bin/python scripts/run_load_validation.py hard-wwii \
  --timeout 600 --out runs/round3-600s/jev-hard-wwii.json
```

延长时限和浏览器修复同时发生，且模型选择存在波动。因此这轮用于确认修复后能否完整运行以及是否到达目标，不能单独测出“多给 300 秒”的净效果，也不能证明决策效率提高。
