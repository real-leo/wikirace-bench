# 三模型统一输入与选择协议

协议版本：`unified-choice-v1`。本轮把 Jev、Laya MLX、SemIf 的任务规则、可见信息和
分组策略统一，使用独立的对照入口；旧模式和历史报告保留以便复现。

## 三者共享的规则原文

```text
Reach the specific goal article in as few link clicks as possible. Choose the exact
goal if offered. Otherwise choose the single next link most likely to lead toward
that specific article. Use the goal description to identify its location,
organization, event series and other distinguishing facts. Shared words or a broad
shared topic alone are weak evidence. Prefer concrete entity connections and
relevant lists; broader hubs are useful when they offer a credible route. Title
similarity is not required. Do not invent intermediate links. All options are
rendered links from the current article; visited and blocked destinations have
already been filtered. Choose exactly one listed option.
```

这里为阅读添加换行；实际字符串只有空格，来自唯一的 `INSTRUCTION` 常量。
可直接查看 [DNA 实际 255 候选输入](2026-09-21-unified-prompt-example.json)，其中包含完整规则和全部选项。

## 信息与选择条件

| 项目 | 三者统一的设置 |
|---|---|
| 目标 | 完整标题；简介最多 280 字符 |
| 当前文章 | 完整标题；简介最多 280 字符 |
| 历史 | 最近六个访问标题，完整保留 |
| 候选 | 完整标题；页面附近文本最多 48 字符；附近文本与标题完全相同时省略 |
| URL 与旧评分 | 执行器保留 URL，模型输入中不提供 URL 或旧模型评分 |
| 答案编号 | 相同的 255 个字母编号；与 SemIf tokenizer 的单-token 答案槽对应 |
| 候选覆盖 | 所有合规、未访问、未封禁的链接参加第一轮，无某个模型独有的评分预筛选 |
| 分组 | 按页面原顺序，每组最多 255 项；每组胜者进入下一轮 |
| 跨组处理 | 重新比较胜者，不直接比较不同组的条件概率 |
| 精确目标 | 三者都通过指令要求优先选择，代码不替模型直接选中；单候选组轮空 |
| 预算 | 每题最多 3,600 秒，30 分钟未完成则沿同一轨迹继续；无点击数上限 |

空白统一规范化，简介和附近文本使用同一套字符截取。标题不会为某个后端单独缩短。
字符上限不等于 token 上限。同一段内容在两个 tokenizer 中的 token 数不同，这是正常现象。

每个分组都由同一个规划器同时检查 Laya 和 SemIf 的完整编码输入。Laya 总长最多 8,192、
指令与选项区最多 7,800；SemIf 总长最多 8,192。超限时，三者都按同一规则缩组，
不丢候选、不为某一模型单独裁剪内容。极长的单个候选无法完整容纳时显式报错。

## 必要的后端格式差异

- Jev：将相同 evidence 作为 `state`，规则作为 Choice `instructions`，候选描述作为 criteria。
- Laya：使用相同 state、instructions、criteria，保留原有 `[CLS]/[SEP]/[MASK]` 布局。
  除提高输入预算，还在当前实例上取消上游每个选项 48 tokens 的隐式裁剪；完整输入先审计再推理。
  不修改模型文件、权重或第三方安装源码。
- SemIf：将相同 state 放入 `evidence`、规则放入 `criterion`、相同编号和描述放入 `options`。
  原生聊天模板关闭 thinking；系统消息只规定根据给定标准选择一个编号、不输出解释。
  仍读取下一-token logits，扩展编号表只在当前实验进程中生效。

三者的核心规则和证据一致；模型架构、tokenizer、服务端包装、推理机制和运行位置不同。
Jev 服务端内部提示词不可见。因此这是条件更接近的系统比较，不是同权重或同算力实验。

## 固定输入核验

[输入快照](2026-09-21-unified-fixtures.json)来自之前保存的三个完整首步页面，分别包含
456、731、1,141 个合规候选，没有经过 Jev 评分筛选。固定 255 项检查取每页原顺序的前 255 项；
全页检查则让全部候选参加淘汰比较。

每个固定 255 输入计时三次，取中位数；计时前预热小请求和 255 项请求，模型加载独立记录。
每个页面另做三个合成目标识别检查，把目标标题放在第 1、128、255 位。
替换项沿用原来的普通 ID，不使用 `injected-exact-goal` 等提示答案的特殊 ID。
这些检查只用于基础诊断，不能视为真实 WikiRace 成功率。

每次原生请求保存共享输入、实际请求、完整编码审计、选择结果和耗时。
共享输入有 SHA-256；Laya 和 SemIf 的实际 token ID 必须与公共规划器计算的一致。
控制器在三者固定测试完成后，核对固定 255 输入、九个控制输入，以及完整页面首轮每组的哈希。
全部一致后才进入完整三题测试。后续轮次和完整比赛的路线可能因模型选择不同而分叉。

## 运行

使用已下载的模型和已有两个独立环境，不需要再次下载：

```bash
.venv/bin/python scripts/run_unified_comparison.py \
  --out-root runs/unified-255/new-run --timeout 3600
```

输出目录必须不存在。固定输入检查按 Jev、Laya、SemIf 顺序执行；完整题目按 Jev、SemIf、Laya 顺序执行。
Jev 子进程使用 `.venv-semif`，仅为复用 tokenizer 依赖；调用的仍是远程 Jev API，不加载本地权重。
只验证固定输入可添加 `--probe-only`。

中断后可用 `--resume` 恢复同一个输出目录。恢复时校验运行时代码指纹，保留已完成的成功/超时结果，
把失败或中断尝试移入 `attempts/` 后再执行相应任务。共享输入不一致等内部错误仍会停止后续派发；
单个远程 API 或页面加载错误保留为错误结果，并允许其他任务继续。失败尝试的点击和耗时单独列入报告。

```bash
.venv/bin/python scripts/run_unified_comparison.py \
  --out-root runs/unified-255/2026-09-21-v3 --timeout 3600 --resume
```

独立后端入口：

```bash
HF_HUB_OFFLINE=1 .venv-mlx/bin/python scripts/run_unified_retest.py \
  --brain laya-mlx --phase probe --out-dir runs/unified-255/new-probe
HF_HUB_OFFLINE=1 .venv-semif/bin/python scripts/run_unified_retest.py \
  --brain semif --phase race --timeout 3600 --out-dir runs/unified-255/new-race
```

普通 `run.py` 和旧复测脚本继续对应历史模式；新的公平对照应使用上面的专用入口。
完整比赛汇总目录为 `runs/unified-255/2026-09-21-v3/`；固定输入检查复用 v1 的完整结果，
93 次已保存调用在 Unicode 分词修复后全部重查，输入与 token 审计未变。完整比赛重新执行。
v2 的 SemIf 第一题因梵文分词不一致中止，第二题随后被主动中断；旧日志保留，不算作模型能力结果。
所有来源均记录在 manifest，不回填旧报告。
异常记录与本轮结果详见 [统一条件对照](2026-09-21-unified-comparison.md)。
机器可读的 [当前汇总与输入审计](2026-09-21-unified-summary.json) 使用 `complete` 和 `pending`
明确区分已完成结果与仍在运行的题目。

验证命令均指定本仓库的 `tests/`，避免把下载的第三方仓库测试一起收集：

- `.venv/bin/python -m pytest -q tests`：87 passed，2 skipped。
- `.venv-mlx/bin/python -m pytest -q tests`：104 passed，1 skipped。
- `.venv-semif/bin/python -m pytest -q tests/test_unified_protocol.py`：9 passed，1 skipped。
- 增加 HTTP 失败证据记录后，`.venv-mlx/bin/python -m pytest -q tests/test_unified_protocol.py`：10 passed，1 skipped。
- 修复 Unicode 预分词后，相同协议测试在 SemIf 环境为 11 passed、1 skipped，在 MLX 环境为 10 passed、2 skipped。

不同环境覆盖重叠，以上数量不能相加。检查包括真实 tokenizer 的聊天边界、255 个独立答案槽、
Laya 原始序列布局一致性、超长标题保留、全候选覆盖、非法输出拒绝、截止时间和部分调用记账。

代码：`wikirace/unified_protocol.py`、`wikirace/unified_brain.py`；
答案编号与聊天模板契约：`data/unified_answer_codes.json`；回归测试：`tests/test_unified_protocol.py`。
