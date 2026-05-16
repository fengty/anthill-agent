# Anthill 蚁国

> 给 Anthill 一句话指令，它把任务拆解、把每一步派给最擅长的模型，再把结果合成。
> 然后它会记住，下一次做得更好。

[English README](README.md)

---

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/fengty/anthill-agent/main/scripts/install.sh | bash
```

然后：

```bash
export ANTHILL_DEEPSEEK_KEY="sk-..."
anthill                  # 进入交互式 REPL
```

安装脚本会自动检测 Python 3.9+，clone 到 `~/.anthill-agent/`，建立独立的
venv，并把 `anthill` 命令丢到 `~/.local/bin/`。重新执行脚本即可升级到最新版。

---

## 三种使用方式

**1. 终端 REPL**

```bash
$ anthill
Anthill — default (3 citizens)
» 用一句话解释什么是信息素路由
信息素路由是路由器根据 agent 历史成功的轨迹累积来选择执行者……
```

**2. 一次性 CLI 命令**

```bash
anthill ask "调研 3 个开源 LLM，对比它们的优劣，给出选型建议"
```

**3. 飞书 / 企业微信 / Telegram / Slack —— 让国家在 IM 里回话**

```bash
pip install 'anthill-agent[daemon]'

# 飞书
export ANTHILL_LARK_APP_ID=cli_...
export ANTHILL_LARK_APP_SECRET=...

# 企业微信
# export ANTHILL_WECOM_CORP_ID=...
# export ANTHILL_WECOM_CORP_SECRET=...
# export ANTHILL_WECOM_AGENT_ID=1000002

anthill serve
```

把 bot 的 webhook 指到 `http://your-host:8765/lark/webhook`（其它通道改路径
即可：`/wecom/webhook`、`/telegram/webhook`、`/slack/webhook`）。**所有
渠道共享同一个 nation——同一份记忆、同一套文化。**

---

## Docker 部署

```bash
cp .env.example .env  # 填入你的模型 key 和 IM 凭证
docker compose up -d
```

`anthill-state` 卷会保留 nation 的状态（公民、信息素、历史、文化）跨重启不丢。

---

## 它和别的 AI 工具有什么不同？

没有一个模型在所有任务上都最优。Claude 推理稳，DeepSeek 中文便宜 10 倍，Kimi
上下文最长，GPT 工具调用最可靠，Gemini 视觉最强。

今天你只能选一个。你订阅 Claude，然后用它处理所有事情——包括它做得最差的那些。

**Anthill 把一个请求拆成多个子任务，让多个模型协作完成——每个模型负责它最
擅长的那一步。** 研究任务交给长上下文专家，代码评审交给推理专家，翻译交给
最便宜可靠的，最后由一个合成步骤把所有结果整合成一个答案。

**你问一次，六个模型协同工作，你看到一个答案。**

而且它会越用越好。系统会从经验中学到：哪个模型实际上最擅长什么、你的工作流
需要哪些子任务、你喜欢什么风格的答案。

---

## 一次请求长什么样

```bash
anthill ask "调研 3 个国产开源大模型，对比强项，给我一页选型建议"
```

内部发生了什么：

```
Scout 拆解                4 个子任务
   ↓
research        → Kimi      (2M 上下文，最适合资料摄入)
compare         → Claude    (跨多源推理最稳)
draft           → DeepSeek  (中英文写作便宜流畅)
polish          → GPT       (格式、长度等约束执行最好)
   ↓
依赖链路传递上下文，前一步输出自动作为后一步的输入
   ↓
你看到一份最终文档
```

你没有给任何人分配任何角色。是国家自己选的。

下次你问类似的问题，路由会更快——这次的信息素轨迹会传递下去。三个月之后，
你的国家会沉淀出一套稳定的偏好：哪个模型处理哪类子任务，**校准到你自己的
工作流，不是榜单**。

---

## 内置功能一览

| | |
|---|---|
| **多模型协作** | DeepSeek、MiniMax 默认接入，OpenAI 兼容协议的都能接 |
| **Plugin** | `web_fetch`、`web_search`、`file_read/write/list`、`pdf_read`、`docx_read`、`xlsx_read`、`shell`（默认关闭）、`code_exec`（默认关闭）、`browser_render`、`browser_screenshot` |
| **IM 渠道** | 飞书、企业微信、Telegram、Slack —— 一个 daemon 四个 webhook |
| **MCP 支持** | 内置 MCP server（任意 MCP 客户端可调）+ MCP client（消费第三方 MCP） |
| **记忆** | 情景检索 + 工作流模板 + 计划缓存 + 信息素 + 事实蒸馏 |
| **可观测** | `anthill power`（六维国力）、`anthill costs`（token 花销）、`anthill history` |
| **可携带** | `anthill export` 打包成 tar.gz，`anthill import` 在另一台机器恢复 |

---

## 路线图

正在 v0.1.x MVP 开发：

- 已完成：一键安装、REPL、9 plugin、4 IM 渠道、MCP、Docker、benchmark
- 进行中：中文文档、PyPI 元数据
- 之后：种群进化（citizen 出生/死亡/变异）、语音渠道、更多 MCP 工具

---

## 许可

MIT。用它、改它、forke 它、证明它错。
