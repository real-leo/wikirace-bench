# 全链接分组选择与 60 分钟 WikiRace 对照

2026-09-21，在同一台 Apple M1 Max / 32 GiB / macOS 15.7.2 上完成九次正式运行。
Jev 使用远程 API；SemIf 和 Laya MLX 使用 Apple GPU，后端依次运行。

**本次结果：Jev 3/3 成功，SemIf 2/3，Laya MLX 2/3。**
全链接分组方案能运行，512-token 限制不必导致候选被丢弃。
延长时间确实帮助 Laya 完成了 Music：30 分钟仍未完成，约 32 分钟成功；
但 SemIf 的 Music 和 Laya 的 WWII 在 60 分钟后仍未完成。
这是三道题各一次的系统对照，不能据此估计稳定成功率或单独比较模型权重的能力。

## 正式结果

表格为“结果；点击数；动作时间”，模型加载不计入动作时间。

| 任务 | Jev | SemIf / Qwen3.5-4B | Laya MLX |
|---|---|---|---|
| DNA → Manipuri pony | 成功；5；73.468 秒 | 成功；9；430.782 秒 | 成功；84；585.130 秒 |
| Music → 2001 AAA Championships | 成功；15；153.962 秒 | 超时；74；3600.227 秒 | 成功；254；1919.776 秒 |
| World War II → Bald Mountain Recreation Area | 成功；4；117.704 秒 | 成功；8；580.147 秒 | 超时；401；3600.175 秒 |

每题预算 3,600 秒，成功提前结束，没有点击次数上限。超时后的极少量记录/清理开销
使保存时间略高于 3,600 秒。Laya WWII 的最后一次已执行点击到达 U.S. state；
之后虽然算出了 Western United States，但预算耗尽，没有执行该点击。

正式完整日志：`runs/grouped-long/formal-articles/`。
可提交的机器可读摘要：[grouped-long-summary.json](2026-09-21-grouped-long-summary.json)，
包含完整路径、分阶段耗时、代码指纹、检查点与输入审计。

## 延长时间究竟有没有帮助

以下检查点来自**同一条连续轨迹**，没有在 30 分钟重新开局。

| 后端 | 10 分钟内成功 | 30 分钟内成功 | 60 分钟内成功 |
|---|---:|---:|---:|
| Jev | 3/3 | 3/3 | 3/3 |
| SemIf | 2/3 | 2/3 | 2/3 |
| Laya MLX | 1/3 | 1/3 | 2/3 |

| 未在 10 分钟内完成的运行 | 10 分钟检查点 | 30 分钟检查点 | 最终 |
|---|---|---|---|
| SemIf Music | 24 点击；Digital preservation | 51 点击；American Revolutionary War | 60 分钟超时；74 点击；Diamond League |
| Laya Music | 91 点击；United States | 237 点击；1995 NCAA Division III men's basketball tournament | 约 32 分钟成功；254 点击 |
| Laya WWII | 95 点击；Ancient Rome | 243 点击；North Korea and weapons of mass destruction | 60 分钟超时；401 点击；U.S. state |

Laya Music 早在第 7 次点击就到过 Track and field，之后离开田径路线。
第 252 次点击通过另一个链接重新到达该页，再经 AAA Championships 完成目标。
时间延长给了它继续探索的机会，同时也暴露了大量绕行。

SemIf Music 最后几分钟才经 Sport in England → Alexander Stadium →
British Grand Prix (athletics) → 2022 Birmingham Diamond League →
2022 Diamond League → Diamond League 回到赛事方向，未能在预算内完成。

## 512 tokens 如何处理全部链接

用户提出的“分组比较，再比较胜者”已经实现。流程为：

1. 读取当前页面所有合规、未访问链接。
2. 按实际 tokenizer 检查完整请求长度，动态缩小分组。
3. 每组只选一个胜者；所有胜者进入下一轮，直至剩一个链接供点击。

没有用代码直接选择精确目标，只有单候选组轮空。
不同组的概率是针对不同备选集合的条件概率，因此**不直接跨组排序概率**，
而是让胜者重新比较。这保证覆盖，不保证正确选择，也不保证最短路线。

后续的 [255 项容量实验](2026-09-21-wide-choice-analysis.md) 已验证可以提高 Laya 的
总输入/选项区预算，并扩展 SemIf 的答案编号。这里的 512 / 16 是本轮长测采用的设置，
不是不可调整的底层上限；后续实验的速度与质量结果单独记录，不回填本表。

固定每组 64 项并不适合 Laya。用上一轮保存的 DNA 快照（456 候选）检查当前提示格式：
8 项可以完整保留；16 项需要 642 tokens，32 项 1,098 tokens，64 项 2,079 tokens。
这不是所有页面的固定容量，实际组大小仍需按 token 检查。
“一个批次运行 64 个独立评分问题”与“一个 512-token 请求完整比较 64 条链接”也不同。

| 后端 | 本轮候选策略 | 每次原生比较 |
|---|---|---|
| Jev | 全量 Score → 前 255 → 原生 Choice | 至多 255 项 |
| Laya MLX | 全部合规链接参加淘汰式比较，无 Score 预筛选 | 至多 8 项，超限缩组，最多 512 tokens |
| SemIf | 全部合规链接参加淘汰式比较，无 Score 预筛选 | 至多 16 项，应用输入上限 4,096 tokens，超限缩组 |

Laya 完整标题保留在 state，选项标题预览及可选简介按显式预算缩短；没有隐式截断模型输入。
SemIf 使用上游 SerialPrefixScorer 和 Qwen3.5-4B 原始精度，对同组选项评分。
两种本地后端的提示、组大小、上下文长度与内部推理不同，不能视为原生 255 选一的等价实现。

## 慢在哪里

两种本地后端均已使用 Apple GPU。本轮现象不能简单归因于“没有 GPU”。

下表单位均为秒。“决策”包括 Score/Choice 的适配和推理，
“网页执行”包括点击与导航等待；少量剩余时间为记录等开销。

| 后端 / 任务 | 决策 | 网页执行 | 页面观测 |
|---|---:|---:|---:|
| Jev DNA | 34.936 | 36.275 | 2.212 |
| Jev Music | 71.307 | 75.609 | 6.730 |
| Jev WWII | 73.357 | 41.659 | 2.613 |
| SemIf DNA | 368.271 | 56.388 | 6.109 |
| SemIf Music | 3073.035 | 467.217 | 59.002 |
| SemIf WWII | 508.013 | 62.425 | 9.686 |
| Laya DNA | 292.824 | 265.499 | 25.859 |
| Laya Music | 780.264 | 1038.725 | 99.525 |
| Laya WWII | 1410.736 | 1950.412 | 234.335 |

SemIf Music 的决策占总时间约 85%；其中 3,727 候选的 List of political families
单页决策用时 443.525 秒。Laya 单页比较较快，例如第三题的 2,855 个候选用时
38.957 秒，但 Music 与 WWII 的大量绕行使网页执行时间超过决策时间。
这两个例子来自不同页面，不能当作严格的相同输入吞吐测试。

模型预加载单独记录：SemIf 119.005 秒，Laya MLX 9.309 秒。
Jev 实际响应版本为 `jev-1.13.0`。本地环境、源码和模型的固定版本见
[部署与 255 候选重测记录](2026-09-21-page-255-retest.md)。
SemIf 源码已下载到 `third_party/SemIf`，约 9.34 GB 的 Qwen3.5-4B 已经代理下载到
`models/Qwen3.5-4B`，文件校验记录见 [下载清单](2026-09-21-semif-download.json)。
模型、环境和完整大日志均保留在本地，不纳入 Git。

## 完整性与对照限制

| 本地运行 | 原生模型调用 | 完成决策中候选出现次数 | 最高实际输入 tokens |
|---|---:|---:|---:|
| SemIf DNA | 226 | 3,188 | 1,875 |
| SemIf Music | 1,897 | 27,079 | 2,082 |
| SemIf WWII | 316 | 4,553 | 1,667 |
| Laya DNA | 3,599 | 24,499 | 512 |
| Laya Music | 9,848 | 66,574 | 512 |
| Laya WWII | 16,966 | 114,807 | 512 |

候选出现次数按每个已完成决策的第一轮计数，同一链接在不同页面/步骤出现会重复计数，
也包括已经算完但未能在截止前执行点击的决策。不是全局唯一 URL 数。

审计已核对九次运行的代码指纹、3,600 秒预算、零滚动、合法动作和候选 URL；
六次本地运行还核对了全部候选进入原生请求、每轮胜者合法、完整标题与正确目标保留、
无隐式 token 截断。所有检查通过。新增覆盖、超时、检查点与红链接回归后，95 项测试通过。

实时网页没有冻结，首步候选也存在以下差异：

| 任务 | Jev | SemIf | Laya |
|---|---:|---:|---:|
| DNA | 567 | 456 | 456 |
| Music | 731 | 731 | 731 |
| WWII | 1,141 | 1,141 | 1,141 |

DNA 首步 SemIf 的标题集合是 Jev 的子集，多出的 111 项主要是遗传学相关链接。
网页可见性/加载状态变化的具体原因尚未单独定位；其他步的候选也没有冻结。
因此本轮是实际系统效果观察，不是严格固定输入的模型排名。
各组划分依赖链接顺序，删减或增补链接也可能改变胜者。
与早先 10 分钟运行相比，还改变了筛选策略和环境过滤，不能把全部差异只归因于时间延长。

## 环境修复与正式版本

先前长时间运行发现环境错误地把 `?action=edit&redlink=1` 的不存在文章链接当作候选，
导致 Laya 点击后收到 HTTP 404。三者候选均受影响，因此修复后全部重跑，旧记录不混入正式表格。
旧结果与故障证据移至 [已排除记录](2026-09-21-grouped-long-superseded.md)。

JavaScript 过滤红链接样式与编辑 URL，Python 再独立校验参数。
正式过滤版本：`article-links-v3-no-redlinks`；九次运行代码指纹一致：
`da49454f820bfd8d9789a15d04d42d2602754f3d8a55e790abefee0911055207`。
正式候选 URL 审计中红链接/编辑链接出现次数均为零，运行期间未修改执行逻辑。

## 复现

先按部署记录准备 `.venv`、`.venv-semif`、`.venv-mlx` 和本地模型；
Jev 读取仓库 `.env` 的 API 配置。输出目录必须不存在。

```bash
.venv/bin/python scripts/run_long_comparison.py \
  --out-root runs/grouped-comparison-another-run --timeout 3600

.venv/bin/python scripts/summarize_page_retest.py \
  runs/grouped-comparison-another-run/jev \
  runs/grouped-comparison-another-run/semif \
  runs/grouped-comparison-another-run/laya-mlx \
  --out runs/grouped-comparison-another-run/summary.json
```

三个后端依次运行，避免 GPU 争用。每个后端保存完整 episode JSON、精简 summary JSON、
逐步 progress JSONL 和控制台日志。根目录 manifest 记录命令、子进程、起止时间以及任务/runner 哈希。
