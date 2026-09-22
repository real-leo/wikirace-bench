# Mac 本地模型部署与新增评测数据复核

日期：2026-09-21。评审代码：`5b7999e`（本次已从 `origin/main` fast-forward 同步）。

## 结论

**Laya-MLX 可以部署，已下载并在当前 M1 Max GPU 上完成离线推理。**
SemIf 现在也支持原生 MLX，具备在这台机器上部署的基础条件，但本次尚未安装或实测 SemIf。

原 Laya CPU 测试的速度问题确实存在；更严重的是 WikiRace 请求超过了 Laya 的输入预算，
导致候选内容丢失。直接切换 MLX 不会修复这个问题。现有数据能说明当前 Laya 系统表现不佳，
**不足以把差异归结为模型本身的导航能力**。

## 1. 新数据复核

| 条件 | Music → 2001 AAA | WWII → Bald Mountain |
|---|---|---|
| Jev page，600s | 成功，6 点击，动作阶段 129.5s | 成功，4 点击，动作阶段 152.2s |
| Laya CPU page，600s | 超时，5 点击，601.3s | 超时，0 点击，613.7s |
| Laya CPU page，无超时 | 62 点击，6962.8s，候选为空结束 | 41 点击，7262.7s，编码错误结束 |

来源：[新增简报](2026-09-21-blog-data-brief.md)、[Jev page 汇总](2026-09-20-page-mode-summary.json)。
本机有 Jev 完整 trace；简报引用的 `runs/laya_page_*.json`、
`/workspace/laya-deploy/smoke_test.log` 未随 Git 提交，也不在当前 Mac 上。
上表 Laya 数字是对简报的引用，非本次独立重算。每条件仅单次结果，不能报告稳定成功率。

- WWII 的零点击与首页约 1149 链的 CPU 打分负担一致。
- 无超时仍漂移说明增加时间不能修复当前系统，但不能据此排除输入适配缺陷。
- 404 是环境错误；`TextEncodeInput` 的准确根因需要原始异常 trace，不能只凭异常名确定是空 title。
- Jev 的整页结果有价值，但与旧 viewport 同时改变了观测和提示词，不能仅归因于模型。

## 2. 共用 page_policy 不代表输入相同

`wikirace/brains.py:LayaBrain._post` 会把超过 64 项的 Choice 截为 64 项。
Jev 对 65–255 个合法候选会直接看到全部候选。没有 Score 时，除精确目标优先外，
Laya 候选通常按原顺序保留。日志 `choice_candidate_count` 和请求在 `_post` 外计算/保存，
也不能证明 Laya 收到了记录中的所有选项。

英文 checkpoint 配置为 `max_len=512`、`head_max_len=192`。
Laya 按“问题 + 选项 + state”构造输入，按 token 预算截断。
已静态检查 PyPI `laya==0.3.4` 的 `laya/common.py:build_sequence`：
其选项压缩、state 截断算法与此次 MLX 版本一致。

把真实 Jev WWII 第一步请求重放到英文 Laya MLX 的 prepare/分词流程，结果如下：

| 检查 | 实测 |
|---|---|
| 首批 Score 共享 state | 3959 tokens，只保留 316 tokens |
| 64 道 Score 各自要评的候选 ID | 仅 2/64 在保留的 state 中出现 |
| 保留的候选 | `L001`、`L002`，第二条 context 仍被截断 |
| 64 项 Choice 各自完整标题 | **0/64** 保留 |
| 前三个选项实际可见文本 | `L1064`、`L1061`、`L966` |
| Choice 指令残片 | `choice question: {"question": "Which` |

64 个选项挤入 192-token head 时，每项被压到约 4 tokens，基本只剩编号。
Score 把 64 个候选放进共享 state；重复前向计算不代表每道问题都看到了对应候选。
GPU 能加速计算，不能恢复信息。

这是对真实请求格式的受控重放，**不是**对缺失的旧 Laya CPU episode 的逐步复现。
但它已直接证明当前适配存在信息丢失，足以阻止“同输入下 Laya 导航能力更弱”的归因。

## 3. 两个项目能否部署

| 项目 | 实现与资源 | 当前判断 |
|---|---|---|
| Laya-MLX | 421M 英文 encoder + 决策 head；FP16 权重约 843MB；512-token 输入 | 已实测可用，适合先替换现有 Laya 后端 |
| SemIf MLX | Qwen3.5-4B 直接读选项 logits；源 checkpoint 约 9GB | 原理上适配 M1 Max 32GB，仍需安装与小批量验证 |

Laya-MLX 是现有 Laya 的移植；SemIf 是另一模型基线，需另适配 Score/Choice。
SemIf 的 direct/serial/shared 支持 MLX，reranker 不支持；依赖固定 MLX 0.32.2
和指定 MLX-LM Git revision。MLX extra 仍保留 Torch 等基础依赖，宜另建环境。
shared 批量与长输入会增加内存占用；32GB 总内存不是全部可供模型使用。
下一阶段应先 direct/serial、小批量验证，再测试 shared。

来源：[Laya-MLX](https://github.com/mizorewww/laya-mlx)、
[Laya 权重](https://huggingface.co/aac6fef/laya-mlx)、
[SemIf MLX 文档](https://github.com/TheoLeeCJ/SemIf/blob/master/docs/MLX.md)、
[SemIf 依赖](https://github.com/TheoLeeCJ/SemIf/blob/master/pyproject.toml)。

## 4. 已完成的本地部署与实测

- 机器：Apple M1 Max，32 GiB，macOS 15.7.2；Python 3.12.13 原生 arm64。
  启动 shell 在 Rosetta 下，但 Python/MLX 是 arm64。
- Metal 可用、实际 device 为 GPU，矩阵运算与模型离线推理均通过。
- 环境：`.venv-mlx/`；依赖固定在 `requirements-mlx.lock.txt`。
- 模型：`models/laya-mlx/`，12 文件合计 **846,231,875 bytes**（约 0.85GB）。
- 代理：`http://127.0.0.1:7890`；固定 HF revision，禁用 Xet 下载路径。
- revision：`047678560251f28113ee8f5df4be82102c7bf336`。
- 权重 SHA-256：`b9c07bf14be2fa5c78a9193a3e6d840ac80e89e62fc40f425834c3d8a6eaa3de`。
- manifest 全部 11 个文件的大小、SHA-256 核对通过（manifest 本身为第 12 个文件）。
- `models/`、`.venv-mlx/` 已加入 `.gitignore`，权重仅留在本地仓库目录。

配置：FP16、GPU、batch size 16，compile/prompt cache 关闭，MLX allocation cache 512MiB。
每例预热一次，计同步完成的 predict，包含准备、分词、推理、结果格式化，不含加载。

| 用例 | 重复次数 | 中位数 |
|---|---:|---:|
| 短客服 Choice | 10 | 18.627ms |
| 短客服 Score | 10 | 21.485ms |
| 紧凑 WikiRace 三选一 | 10 | 18.734ms |
| 原始 WWII 首批 64 道 Score | 3 | 4669.955ms |
| 原始 WWII 64 项 Choice | 3 | 89.488ms |

模型加载此次为 0.314s（哈希校验刚读过文件，受系统文件缓存影响，不能当冷启动）。
probe 峰值 MLX allocation 1657.89MiB，非进程 RSS。
短例正确选择 billing、Caffeine；原始长请求计时仅用于诊断，不能视为有效选路测试。
短请求毫秒数不能外推到千链接整页，也不能与不同机器、不同请求的 CPU 烟测计算严格加速比。

原始测量：`runs/laya-mlx-deployment/probe.json`；
[可提交摘要](2026-09-21-mac-mlx-summary.json)。

## 5. 复跑与接入顺序

在仓库根目录执行：

```bash
HF_HUB_OFFLINE=1 .venv-mlx/bin/python scripts/probe_laya_mlx.py

# 有本机 Jev trace 时，审计实际输入并测长请求：
HF_HUB_OFFLINE=1 .venv-mlx/bin/python scripts/probe_laya_mlx.py \
  --trace runs/round4-page/jev-hard-wwii.json \
  --out runs/laya-mlx-deployment/probe.json
```

另一台兼容 Mac 重建：

```bash
uv venv --python 3.12 .venv-mlx
uv pip sync --python .venv-mlx/bin/python requirements-mlx.lock.txt
HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 \
ALL_PROXY=http://127.0.0.1:7890 HF_HUB_DISABLE_XET=1 \
HF_HUB_DISABLE_IMPLICIT_TOKEN=1 HF_HUB_DOWNLOAD_TIMEOUT=120 \
.venv-mlx/bin/hf download aac6fef/laya-mlx \
  --revision 047678560251f28113ee8f5df4be82102c7bf336 \
  --local-dir models/laya-mlx
```

当前 `run.py --brain laya` 仍走原 PyTorch 适配器。本次完成下载、隔离环境、离线部署验证和输入诊断。

后续应先：

1. 每条 Score 明确携带该候选标题/上下文及目标，按 token 预算构造短输入；
   断言目标候选仍在最终 tokens 中，保留全候选覆盖。
2. Choice 保留完整短选项，按实际 token 预算控制组大小。
   如采用分组选择或先评分缩减，必须记为新策略，不能声称等价于 Jev 的 255 项直选。
3. 日志记录后端真实候选、截断统计、device/dtype/model revision、提示版本。
4. 先验证一跳目标、候选末端目标，再重复难任务；时间、输入覆盖、导航结果分别报告。

Blog 建议改述：当前 CPU Laya 系统在难任务上耗时高、未达目标；
后续审计发现候选截断与集合差异，尚不能据此评价 Laya 模型本身的导航上限。
