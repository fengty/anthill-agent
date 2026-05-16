# 快速开始

5 分钟把 Anthill 跑起来。

---

## 1. 安装

```bash
curl -fsSL https://raw.githubusercontent.com/fengty/anthill-agent/main/scripts/install.sh | bash
```

需要：

- Python 3.9+（脚本会自动找 3.9 - 3.13）
- git
- macOS 或 Linux（Windows 走 WSL）

安装完后，确认 PATH 上有 `~/.local/bin`：

```bash
echo $PATH | grep -q "$HOME/.local/bin" && echo OK || echo "把 export PATH=\"\$HOME/.local/bin:\$PATH\" 加进 ~/.zshrc"
anthill --version
```

---

## 2. 配置模型

至少需要一个模型 API key。**推荐 DeepSeek**——便宜（缓存命中 0.028 元/百万 token），中文好，国内可达。

```bash
export ANTHILL_DEEPSEEK_KEY="sk-..."
```

可选：再加一个 MiniMax，让多模型协作发挥作用。

```bash
export ANTHILL_MINIMAX_KEY="..."
export ANTHILL_MINIMAX_GROUP="..."   # MiniMax 控制台的 GroupId
```

把这些导出加到 `~/.zshrc`（或 `~/.bashrc`）一劳永逸。

---

## 3. 第一次对话

```bash
anthill
```

会自动建一个 default nation，spawn 3 个公民。然后直接打字：

```
» 用一句话解释什么是熵
熵是衡量系统混乱程度或不确定性的物理量……

» /trails
（看信息素积累在哪些 citizen 上）

» /power
（看国家成长的六维进度）

» /quit
```

---

## 4. 多步任务

试一个真正能展示多模型协作价值的请求：

```bash
anthill ask "调研一下 RAG、LangChain、LlamaIndex 三个项目，对比它们的差异，给我写一段选型建议"
```

Anthill 会：

1. **拆解**：4 个子任务（research、compare、draft、polish）
2. **路由**：每个子任务被分到合适的 citizen（信息素决定）
3. **执行**：依赖前序输出作为上下文
4. **合成**：最后一步是国王看到的答案

跑完之后看一下：

```bash
anthill power      # 国力进度（解锁了 Statecraft 这一纪元）
anthill costs      # 这次花了多少钱（通常几分钱）
anthill identity   # 国家学到的词汇表
```

---

## 5. 给国王反馈

对结果满意？`anthill rate up` 让 nation 记住这次的协作方式。

不满意？`anthill rate down` 削弱这条路径，下次换人做。

```bash
anthill ask "..."
anthill rate up         # 强化这次涉及的 citizen
```

国王的反馈会通过信息素影响未来的路由。**用得越多，nation 越懂你**。

---

## 6. 接入 IM

让你的 nation 在飞书/企业微信/Telegram/Slack 里回话：

```bash
# 安装 daemon 依赖
pip install 'anthill-agent[daemon]'

# 飞书举例
export ANTHILL_LARK_APP_ID="cli_..."
export ANTHILL_LARK_APP_SECRET="..."

# 启动 webhook 服务
anthill serve
```

然后在飞书开发者后台把事件订阅 URL 指向：

```
http://你的服务器:8765/lark/webhook
```

任何 @机器人 的消息都会进入 nation 处理，结果通过飞书 API 回到群里。

---

## 7. Docker 部署

服务器上长期跑：

```bash
git clone https://github.com/fengty/anthill-agent.git
cd anthill-agent
cp .env.example .env
# 编辑 .env 填入你的 key

docker compose up -d
```

`anthill-state` 卷会持久化所有 nation 状态——重启不丢记忆。

---

## 下一步

- 看一眼 [README.zh.md](../../README.zh.md) 全局介绍
- 看一眼 [benchmark.md](../benchmark.md) 了解为什么"信息素路由"比预设角色好
- 看一眼 [why-anthill.md](../why-anthill.md) 项目的哲学起点
