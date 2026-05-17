# Anthill vs Claude CLI vs Hermes — 深度对照

> **目的**：弄清楚 Anthill 跟两个主要参考系（Anthropic Claude CLI、
> Hermes 多模型框架）相比，**应该补哪些 baseline 短板**，又
> **应该放大哪些差异化优势**。结论直接驱动 0.1.7 之后的迭代规划。

> **数据来源**：Claude CLI 部分基于公开文档 + 公开仓库行为（最近一次
> 校对 2026-05）；Hermes 部分基于用户在之前会话中提供的描述。具体能力
> 边界可能随版本变化，本文聚焦"产品形态层面的对照"，而非逐条 feature
> matrix。

---

## 三个项目的产品定位（一句话）

| | 定位 | 核心隐喻 |
|---|---|---|
| **Claude CLI** | 单一强模型 + 强工具系统的"代理工作台" | 把一个聪明的模型武装成开发者助手 |
| **Hermes** | 多模型 + 多插件的"AI 工具箱" | 给开发者一个能挂插件的 AI 中枢 |
| **Anthill** | 多模型协同的"会成长的国度" | 一个任务 → 多模型分工 → 国度越用越懂你 |

三者**互相不替代**。Claude CLI 在"深度单模型 agent"赛道。Hermes 在
"配置化多工具集成"赛道。Anthill 在"emergent multi-model
collaboration"赛道。

---

## Claude CLI 强在哪（Anthill 该学习的）

### 1. CLI 体感的细节做得极致

- **流式输出**：tokens 边生成边显示，用户感觉"快"，即使总时间不变
- **多行输入**：`"""` heredoc 或 Shift+Enter 自然换行
- **Tab 补全**：slash 命令、文件路径、模型名都能 Tab
- **vim mode**：`prompt_toolkit` 驱动的模式切换 + 高亮
- **状态行**：底部可定制 statusbar
- **会话快照**：随时 `/resume` 回到任何历史会话
- **后台任务**：长任务不阻塞 REPL

### 2. 文件 / 代码库作为"一等公民"

- `@filename.py` 把整个文件附进上下文
- glob 模式 `@src/**/*.py` 拉一组文件
- 项目目录被理解为"工作上下文"，不只是搜索域
- 跟 git 集成：自动看 staged diff、branch 等

### 3. Skill / Slash 命令的扩展生态

- 用户可以**自己写 markdown 文件**定义新 slash command
- Hooks 系统让 plugin 介入 lifecycle (pre-ask、post-ask、on-error)
- MCP 协议跟桌面 AI 工具生态打通

### 4. Computer Use / Tool Use 闭环

- 浏览器、文件、shell、image — 一套统一的 tool calling 协议
- Claude 模型本身对 tool use 训练充分，调用稳定

---

## Hermes 强在哪（Anthill 已经做了或不做的）

### Anthill 已经有的（不再描）

- ✓ curl-bash 一键安装
- ✓ IM 平台对接（Lark / Telegram / Slack / WeCom，daemon 模式）
- ✓ 插件系统（file / web / shell / docs / browser / code_exec）
- ✓ CLI 配置文件 + secrets.toml
- ✓ MCP server + client
- ✓ Workflow / Recipe 模板

### Anthill **故意不做**的（Hermes 走的但 Anthill 不走）

- ✗ **手动模型切换** — Hermes 让用户挑 model，Anthill 让 router 自己学
- ✗ **单 model 单任务** — Hermes 一任务一 model，Anthill 一任务多 model 协同

这是产品哲学差异。**Anthill 的差异化恰恰来自"不做手动切换"**——
那是 Hermes 的舒适区，但是天花板。

---

## Anthill 独有 / 领先的差异化（要放大）

下面这些在 Claude CLI 和 Hermes 里**都没有**：

| 能力 | Anthill 模块 | 用户感受 |
|---|---|---|
| **多 model 协同一个 ask** | scout + executor + ensemble | "翻译这段并解释" — translate 走 deepseek、explain 走 minimax，自动 |
| **Pheromone emergent specialization** | router + pheromone | 用得越多，路由越准；不需要手动告诉系统"谁擅长什么" |
| **公民生命周期 (出生/退休/繁殖)** | lifecycle + reproduction | nation 是活的有机体，僵尸 citizen 自动退休，强者繁衍变种后代 |
| **质量驱动的多轮 deliberation** | deliberate | 不是"模型说 done 就 done"，而是"客观质量分到阈值才停" |
| **多维度开放词汇评分** | values + DimensionCatalog | judge 自己提"correctness / depth / tone"等维度，用户可调权重 |
| **失败归因 + 免疫隔离** | failure + immune | model 突发性挂掉时自动隔离，恢复后试探性放行 |
| **任务复杂度智能判定** | complexity | "你好"秒回，"调研 X"走 deliberation |
| **澄清问题层** | clarify | 模糊请求先反问，避免 garbage-in |
| **多 model 协同可视化** | repl | REPL 显示每个 subtask 走了哪个 model，看见协同发生 |

---

## Anthill 当前的明显短板（baseline 差距）

| 短板 | 用户痛点 | 修复难度 |
|---|---|---|
| **无流式输出** | 长任务时屏幕静止 5-30 秒，体感像挂死 | 中（provider 层改 streaming） |
| **input() 单行** | 粘贴代码 / 长文本会断 | 小（输入层切到 prompt_toolkit 或多行 mode）|
| **无 Tab 补全** | slash 命令 / 模型名要全文输入 | 小（readline 已经支持 completion，需要 hook） |
| **文件作为上下文** | 想问"这个文件怎么改"得手动 cat 进来 | 中（实现 `@file` 语法 + glob）|
| **无 image 输入** | 截图问"这个错怎么修"做不到 | 中（vision provider 支持 + REPL 文件路径 / drag） |
| **启动慢** | 第一次 import 时间 ~1s（rich + click 都重） | 中（lazy import） |
| **无 streaming progress 反馈细节** | 只看到 `running...` 不知道在做什么 | 小（在 attempt 层加内部状态）|

---

## 战略选择 — Anthill 接下来该往哪走

有两条岔路：

### 岔路 A：补 baseline UX，向 Claude CLI 看齐

短期内补完 streaming / 多行 / Tab 补全 / @file 等，让用户**第一次坐到 REPL 前** 不觉得 "这工具糙"。

代价：4-6 个 patch 的工程量，全是模仿别人的活，没有新差异化。

收益：留住第一波用户，让他们有机会看到 Anthill 的独有价值。

### 岔路 B：放大独有差异化

继续往 nation 生命周期 / 多 model 协同上深挖，不管 baseline UX。

代价：用户第一眼觉得 "比 Claude CLI 糙太多"，可能没耐心看到深层价值就走了。

收益：差异化拉满，是个完整的"工具哲学"，远期值钱。

### 真实的最佳选择：A + B 交错

具体节奏：

- **每 1 个 baseline patch (A)** 配 **1 个差异化 patch (B)**
- baseline 的目标是"和 Claude CLI 拉到不丢人的距离"，不追求超越
- 差异化的目标是"让看进来的用户惊到"，不追求面面俱到

---

## 提议的迭代路线（0.1.7 → 0.1.18）

按"A B 交错"安排，全部按 VERSIONING 规则保持 **patch-only**：

| 版本 | 类别 | 内容 | 工程量 |
|---|---|---|---|
| **0.1.7** | A | 流式输出 — provider 层支持 streaming，REPL 边生成边显示 | 中 |
| **0.1.8** | B | `@file` / `@dir/**.py` 语法 — 把文件内容自动塞进 prompt | 中 |
| **0.1.9** | A | 多行输入 — `"""` heredoc 进入 multi-line mode，单独发送 | 小 |
| **0.1.10** | B | Plan 可干预 — Scout 出 plan 后让用户编辑 / 跳过步骤 | 中 |
| **0.1.11** | A | Tab 补全 — slash 命令 / 模型名 / nation 名 | 小 |
| **0.1.12** | B | Nation 绑定工作目录 — `cd /path/project && anthill` 自动加项目上下文 | 中 |
| **0.1.13** | A | 启动优化 — lazy import 把首次启动从 ~1s 降到 ~300ms | 小 |
| **0.1.14** | B | Skill 自动挖掘 — 高频用过的 ask 模式自动转 recipe 让用户确认 | 中 |
| **0.1.15** | A | Image 输入 — 支持 `attach <path>` 把图片送给 vision-capable citizen | 中 |
| **0.1.16** | B | Nation 之间协作 — 一个 nation 在 plan 时能向另一个 nation 求助 | 中 |
| **0.1.17** | A | Custom slash commands — 用户在 `~/.anthill/commands/*.md` 自定义 | 中 |
| **0.1.18** | B | 协同评审模式 — 把 deliberation 的 critic 改成多 citizen 投票 | 中 |

12 个 patch，**所有都按 VERSIONING 保持 minor=1**。理论上做到 0.1.18 → 0.1.19 也都是 patch。
预计 1-2 个月节奏（视个人时间）。

### 关键里程碑：什么时候考虑升 0.2 minor？

按 [VERSIONING.md](../VERSIONING.md) 规则，升 minor 需要满足：
- 新 top-level CLI 命令组，**或**
- 不向后兼容的公开 API / 磁盘格式变更，**或**
- 多版本 arc 收尾，**或**
- maintainer 显式签字"这值得 release-notes moment"

候选的 0.2 触发点（**未来**事件，不是承诺）：

- 0.1.18 完成时，A+B 交错弧整体收尾，可以叫 0.2.0
- 出现"nation 之间的协议"标准（即 0.1.16 的扩展），那是新格式
- 出 web 界面 / VS Code 插件 / desktop app

---

## 不走的路（避免范围爆炸）

明确**不做**这些方向，免得后面再讨论：

- ❌ **IDE 插件 / VS Code extension** — Anthill 是 CLI tool，UI 投资留给社区
- ❌ **代码库索引 / repo embedding** — 那是 Claude CLI / Cursor 的赛道
- ❌ **微调 / fine-tuning** — Anthill 编排模型，不训练模型
- ❌ **GUI 桌面应用** — CLI + 浏览器 dashboard 足够
- ❌ **付费云服务** — 项目保持本地 + 开源

---

## 阅读到这的下一步

如果你（maintainer）认可这个规划：

- **直接开干 0.1.7 流式输出** — A 类，table stakes，让等待时间不再像挂死

如果你想调整：

- 砍掉某个 patch / 调换 A/B 顺序 / 加新方向

任何调整都是 patch 级别的小事，不影响版本号节奏。
