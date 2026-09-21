# WikiRace × Jev × Laya：可引用的真实数据简报

整理日期：2026-09-21。只收录仓库 `reports/` 与本机 `runs/` 中有日志/JSON 支撑的数字。  
仓库：https://github.com/real-leo/wikirace-bench  

**写作时请默认写清：** 下列难任务多为**每个条件只跑 1 次**，不能写成稳定成功率；跨机器/跨代码版本的耗时不宜直接横比。

---

## 1. 系统与部署（事实）

| 项目 | 数据 |
|---|---|
| Bench | DrissionPage 打开英文维基；默认整页观测 `--observation page` |
| 整页策略 | 过滤后 ≤255：直接 Choice；>255：Score 全覆盖（批大小≤64）→ 取 top-64 再 Choice |
| Jev | TypeSafe API，`jev-1.13.0`（page 验证报告） |
| Laya | 本机部署 `convaiinnovations/laya`（英文 checkpoint），`laya==0.3.4`，**CPU**，加载约 32.4s，RSS ≈3.25GB |
| Laya 烟测 | 客服邮件 4 问一次前向 **372.7ms**（department=billing conf 0.8907）；WikiRace 风格选链 **162.8ms**（选 L001 Caffeine，conf 0.1596） |
| 代码演进 | 视口滚动/点击 → 修复 finalist/评分 → 整页 page 模式（Jev）→ Laya 对齐 page（commit `91a2db0`） |

Laya 烟测来源：`/workspace/laya-deploy/smoke_test.log`。

---

## 2. 核心对照：同一套整页规则下，Jev vs Laya（难任务）

规则相同：`--observation page`，只点击不滚动；≤255 直选 / >255 打分再 top-64。

### 2.1 Jev page（验证报告，timeout 600s）— 成功

来源：`reports/2026-09-20-page-mode-validation.md`、`reports/2026-09-20-page-mode-summary.json`

| 任务 | 结果 | 点击 | 滚动 | 动作阶段 | 含准备总耗时 | 路径 |
|---|---|---:|---:|---:|---:|---|
| Music → 2001 AAA Championships | **成功** | 6 | 0 | 129.5s | 185.5s | Music → Performing arts → Entertainment → Sport → Sport of athletics → AAA Championships → **2001 AAA Championships** |
| WWII → Bald Mountain Recreation Area | **成功** | 4 | 0 | 152.2s | 208.5s | WWII → United States → Michigan → List of Michigan state parks → **Bald Mountain Recreation Area** |
| U.S. state → Michigan（检查） | **成功** | 1 | 0 | 32.2s | 83.6s | （一跳） |

API 用量（同报告）：

| 任务 | API 请求 | 输入 tokens | 输出 tokens | Score 耗时 | Choice 耗时 |
|---|---:|---:|---:|---:|---:|
| Music | 43 | 840,893 | 45,423 | 65.1s | 8.5s |
| WWII | 73 | 1,570,495 | 77,290 | 99.3s | 2.5s |

WWII 各步页面链接规模（过滤后全部评分）：约 **1148 / 1833 / 1030 / 302**。

### 2.2 同任务在旧视口规则下的 Jev（对照，timeout 600s）

来源：同上 page-mode 验证报告（改整页前的对照跑）

| 任务 | 结果 | 点击 | 滚动 | 动作阶段 |
|---|---|---:|---:|---:|
| Music → 2001 AAA（viewport） | **超时** | 71 | 75 | 600.1s |
| WWII → Bald Mountain（viewport） | **超时** | 94 | 32 | 600.1s |

**可写进 blog 的一句话：** 同一难任务，从「视口+滚动」换成「整页+只点」后，Jev 在本机单次实跑中从超时变成 4–6 次点击到达；但这是观测方式与提示一起改的，不能单归因于模型。

### 2.3 Laya page，timeout 600s（本机 CPU）

来源：`runs/laya_page_{1,2,3}_*.json`

| 任务 | 结果 | 点击 | 滚动 | 秒数 | 末段路径 |
|---|---|---:|---:|---:|---|
| DNA → Manipuri pony | 超时 | 9 | 0 | 626.1 | … → Semantic Scholar → … → IBM |
| Music → 2001 AAA | 超时 | 5 | 0 | 601.3 | … → ISSN → DOI → … → MIT |
| WWII → Bald Mountain | 超时 | **0** | 0 | 613.7 | 停在 WWII（首页 ~1149 链，CPU 打分耗尽预算） |

### 2.4 Laya page，timeout=0（本机 CPU，看决策上限）

来源：`runs/laya_page_notimeout_{1,2,3}.json`

| 任务 | 结果 | 点击 | 滚动 | 秒数 | 结束原因 / 末段 |
|---|---|---:|---:|---:|---|
| DNA → Manipuri pony | **error** | 26 | 0 | 2047.3（~34min） | 点到 `Tritransitive_verb` 维基 **404**；路径已漂到语法/语言 |
| Music → 2001 AAA | **fail** | 62 | 0 | 6962.8（~116min） | `page_links_empty`；ISSN→MIT→希腊→阿肯色→幼儿园… |
| WWII → Bald Mountain | **error** | 41 | 0 | 7262.7（~121min） | Score 阶段 `TextEncodeInput` TypeError；皇家海军→宪法→田纳西→青铜时代→数学… |

**可写进 blog 的一句话：** 去掉超时后 Laya 仍未到达任一难目标；失败形态是路径语义漂移 + 环境/编码错误，而不是「再多几分钟就能到」。同规则下 Jev 的 Music/WWII 单次成功路径短且主题一致。

---

## 3. 视口时代的补充数据（可选段落）

### 3.1 早期视口 + 滚动计步（聊天/本机 retest，Jev）

来源：本机 `runs/retest*.txt`（与 page 验证不是同一轮配置）

| 任务 | 结果 | 步数 | 点击/滚动 | 秒数 | 备注 |
|---|---|---:|---|---:|---|
| DNA → Manipuri pony | 成功 | 41 | 26/15 | 64.1 | … → Shan Horse → Manipuri pony |
| Music → 2001 AAA | 成功 | 126 | 56/70 | 187.1 | … → 1981 AAA → 2001 AAA |
| WWII → Bald Mountain | 超时 | 183 | 134/49 | 302.2 | 末段 Loveland Pass → Rocky Mountains |

同任务在修复后视口重跑（`runs/fix_jev_*.txt`，timeout 300）三局均超时——说明**视口规则下波动大**，blog 里宜作「早期探索」而非主结论。

### 3.2 用户侧 Jev 验证报告（viewport 难任务，另一套本机环境）

来源：`reports/2026-09-20-jev-validation.md`（timeout 300，含准备时间分列）

| 轮次 | 任务 | 结果 | 点击/滚动 | 动作耗时 |
|---|---|---|---|---:|
| round2 | DNA → Manipuri pony | **成功** | 49/13 | 285.4s |
| round2 | Music → 2001 AAA | 超时 | 36/17 | 306.8s |
| round2 | WWII → Bald Mountain | 环境错误 | 40/19 | 278.2s |

WWII round2 已到 Oakland County, Michigan 一带，随后页面加载失败——**不能当干净的导航失败**。

### 3.3 Laya 视口（CPU，timeout 300）

来源：`runs/laya_{1,2,3}_*.txt` 汇总（聊天记录与文件一致）

| 任务 | 结果 | 步数 | 点击/滚动 | 秒数 |
|---|---|---:|---|---:|
| DNA → Manipuri pony | 超时 | 29 | 20/9 | ~301 |
| Music → 2001 AAA | 超时 | 31 | 25/6 | ~310 |
| WWII → Bald Mountain | 超时 | 40 | 26/14 | ~302 |

CPU 上约 **~10s/步**（视口逐步打分），5 分钟预算主要花在算力而非决策质量。

---

## 4. 机制修复对照（可写「我们踩过的坑」）

来源：`reports/2026-09-20-jev-validation.md` 受控 API 实验（非完整导航）

| 代码 | 最后一屏新目标 Moon 的评分 | Choice 实际候选 | Jev 选择 |
|---|---:|---|---|
| 旧 `90aeb3f` | 0.9950 | 只有 NASA | NASA（错） |
| 新 `f0e5210`+本地修复 | 0.9925 | Moon、NASA | **Moon**（对） |

说明：曾出现「刚打完最高分，finalist 名单却是旧的」；修复后受控对照通过。这是环境 bug，不是模型能力结论。

---

## 5. Blog 可用结论（克制表述）

1. **Jev + 整页候选**在本机单次实跑中，能把 Music / WWII 难任务压到 **4–6 次点击**到达；同任务旧视口规则下 600s 超时。  
2. **Laya（CPU 本地）**对齐同一整页策略后，600s 内仍超时；取消超时后点击更多，但路径持续离题，并出现 404 / 编码器异常等中止。  
3. **不能**把 Laya 失败简单写成「超时太短」；也不能把 Jev 成功写成「官方 Demo 复现」——官方完整 prompt/过滤未公开（见 `reports/2026-09-20-official-demo-comparison.md`）。  
4. 公平比较需要：同观测模式、同任务、尽量同机器；Laya 若继续比决策，宜 GPU 或降低大页全量打分成本。

---

## 6. 原始文件索引

| 内容 | 路径 |
|---|---|
| Jev 整页验证叙述 | `reports/2026-09-20-page-mode-validation.md` |
| Jev 整页精简 JSON | `reports/2026-09-20-page-mode-summary.json` |
| Jev 视口验证 | `reports/2026-09-20-jev-validation.md` |
| 官方 Demo 差异 | `reports/2026-09-20-official-demo-comparison.md` |
| Laya page@600 | `runs/laya_page_*.json` |
| Laya page@无限时 | `runs/laya_page_notimeout_*.json` |
| Laya 部署烟测 | `/workspace/laya-deploy/smoke_test.log` |

---

## 7. 遇到的问题与解决方案（blog 可写「工程故事」）

按时间线与主题整理；凡写「已验证」的，均有报告或受控实验支撑。

### 7.1 评测设计：测的是什么

| 问题 | 现象 | 处理 |
|---|---|---|
| 最初用 MediaWiki 全页 `prop=links` | 与真人「看屏幕」不一致；也不是官方 Demo 的完整复现 | 改为 DrissionPage 真实浏览器 |
| 视口 + 滚动/点击同价 | 模型常空滚或上下晃；难任务易超时 | 迭代：滚动不算步 → 只留下滚 → 最终默认改为**整页候选、只点击** |
| 「滚动硬限制」（链接多就不给 SCROLL） | 表面成功率上升，但是代码替模型做决定，不公平 | **撤销**；保留滚动为合法动作（视口规则时期） |
| 目标描述几乎只有标题复读 | WWII 等题缺少 Michigan 等地理锚，易漂到「山/公园」泛主题 | 统一用维基 intro extract（同长度）；page 提示强调地点/组织/赛事身份（`page-target-v1`） |
| 超时前「整局 top-K 传送门」跳转 | 可能点到当前页不可达的旧链接，改变 WikiRace 规则 | **拒绝**；短名单范围保持在当前页累计候选 |

### 7.2 环境 / finalist 正确性（曾被误判成模型差）

用户在 `90aeb3f` 复现后指出：WWII 超时**不能完全归因于模型**。已修（约 `6c3def9` / `f0e5210`）：

| 问题 | 影响 | 解决方案 |
|---|---|---|
| 最后一屏新分数未进入 finalist | 目标刚打满分，Choice 仍是旧名单 | 统一流水线：`observe → score → ingest → refresh_finalists → choose` |
| 每屏只评前 20 个链接 | 第 21 个即使是目标也不进 top-K | 视口模式改为分批评完所有可见链接 |
| 先 top-K 再过滤已访问 | 前 5 都访问过时名单变空，漏掉合法第 6 名 | **先过滤再取 K** |
| 按 `L001` 跨页黑名单 | A 页拉黑 L001 后，B 页同编号合法链也被杀 | 只按**标题**（规范化）防环，不用跨页 id |
| 点击未严格限制在 offered | 未入选 finalist 的链仍可能被执行 | `candidate_for` / step 只接受本步 offered |
| MediaWiki 拉简介 403/失败 | 目标描述回退成标题复读 | 设置合规 User-Agent（`f0e5210`） |

**受控验证（非完整导航）：** 旧代码最后一屏 Moon 分 0.995 但仍选 NASA；修复后同分档下选 Moon。见 `reports/2026-09-20-jev-validation.md`。

### 7.3 浏览器与运行时

| 问题 | 现象 | 处理 |
|---|---|---|
| 页面加载未就绪就继续 | 导航失败 / 空内容 | 检查文档标识、文章根、标题；失败释放资源（用户侧验证报告） |
| 维基 404 / 不可用页 | 如 Laya 点到 `Tritransitive_verb` → `PageLoadError` | 错误分类记录；局中止；需在执行器侧加强死链过滤（仍可写为已知风险） |
| 模板讨论页等非内容页 | Music 终局 `Template talk:…` → `page_links_empty` | 过滤非文章命名空间（page 模式已强调）；残留说明过滤仍可加强 |
| TLS / API 中断 | Jev Score 阶段 EOF | 连接复用 + 有限重试（认证错误不重试） |
| `avg_score≈0.2` 被当成「路径很差」 | 指标是「整局所有已评分标题」均值，含大量未点击链 | 标明指标含义；增加 `avg_clicked_score` |

### 7.4 Jev / TypeSafe 侧

| 问题 | 现象 | 处理 |
|---|---|---|
| Choice 上限 255 | 大页不能一次塞进所有链接 | 公开架构：>255 先 Score 再短名单 Choice；本项目 shortlist=64 |
| 视口信息不足 | 例：`U.S. state` 视口无 Michigan，但全页有 | 诊断后改为整页候选；提示对照实验：改 focus 后同一节点从「点州政府」变为「下滚」（见 official-demo 报告） |
| 官方 Demo 细节未公开 | 无法声称逐项复现 | blog 写「遵循公开两阶段结构」，并链到差异报告 |

### 7.5 Laya 本地部署与对齐

| 问题 | 现象 | 处理 |
|---|---|---|
| 无 GPU | CPU 推理；视口逐步打分约 ~10s/步 | 部署英文单 checkpoint；避免 Router 三权重全 preload |
| `USE_TF` / transformers 探测 TF | 加载可能挂死 | `USE_TF=0`、`TRANSFORMERS_NO_TF=1` |
| 误用 viewport 跑 Laya | 与「>255 才打分」的 page 加速无关，极慢 | 改为 `--observation page`，与 Jev 同一 `page_policy`（`91a2db0`） |
| Choice 选项过多 | `head_max_len=192`，186+ 选项直接崩 | `_post` 侧短名单截断至 64；单选项 short-circuit（避免 `topk(2)` 崩） |
| 大页全量 Score | WWII 首页 ~1149 链，CPU 打分可超 10 分钟甚至吃光 600s | 工程上可接受为代价；比决策需 GPU 或分层/抽样打分（未做则写「未解」） |
| 取消超时仍失败 | 路径乱逛 + 404 / `TextEncodeInput` | 说明瓶颈主要是**选路质量与稳健性**，不是单纯 timeout |
| `TextEncodeInput` TypeError | WWII 无超时局长跑后 Score 崩 | 记为已知缺陷（空文本/异常 title 进编码器）；待加固输入清洗 |

### 7.6 流程与协作

| 问题 | 处理 |
|---|---|
| Cloud Agent 需 Origin/Cursor SCM；本机 `gh` 已登录但不能代替 | 本地改代码 + `gh` 推 `real-leo/wikirace-bench` |
| 邀请协作 | 已邀请 `datehoer`（write），待对方接受 |
| 难任务日志曾决定暂不提交 | 数据 brief 可先本地用于 blog，按需再推 |

### 7.7 仍可写的「未解决问题」

1. Laya 在同 page 规则下难任务选路仍明显弱于 Jev（单次实跑）。  
2. CPU 上大页全量 Score 成本高；未做分层粗筛。  
3. 死链 / 讨论页 / 空上下文字段仍会导致局中止。  
4. 缺重复试验，不能报置信区间。  
5. 与 TypeSafe 官方 WikiRace Demo 的 prompt/过滤细节仍未知。

