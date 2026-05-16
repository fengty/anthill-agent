# Changelog

All notable changes to Anthill, oldest first.

## v0.1.0 — Hermes-parity milestone (May 2026)

Anthill is now usable the way Hermes is used:

- One-line install via `curl ... | bash`
- Bare `anthill` drops you into an interactive REPL
- Run `anthill serve` and expose a webhook URL for **Lark/Feishu**, **Telegram**, or **Slack**
- Built-in plugins for the web (`web_fetch`, `web_search`), filesystem (`file_read`, `file_write`, `file_list`), and shell (opt-in)
- Multi-model collaboration on every single request — Scout decomposes, pheromone routes each subtask to whichever model is best for it, results are synthesised

Beyond Hermes:

- Pheromone-based routing learns model-to-task fit from real outcomes
- Cultural layer (vocabulary + house style) makes the nation feel like yours
- Statecraft: dependency-aware DAG execution with retries and citizen rotation on failure
- Five flavors of memory (episodic search, semantic facts, workflow templates, plan cache, pheromone trails) — no skill bloat
- Empirical benchmark proves pheromone routing beats role routing by +54.5 pp

---

## v0.0.x — Foundations (laid down in order)

### v0.0.29 — Telegram + Slack channels

Two more inbound IM platforms. `parse_event` per channel, send via Bot API, daemon exposes `/telegram/webhook` and `/slack/webhook`.

### v0.0.28 — Filesystem + shell plugins

Four new built-ins: `file_read`, `file_write`, `file_list`, plus opt-in `shell` with dangerous-pattern blocklist.

### v0.0.27 — Daemon mode

`anthill serve` runs a FastAPI + uvicorn webhook listener. Health endpoint, Lark webhook handler, fire-and-forget message dispatch into `Nation.ask`.

### v0.0.26 — Lark/Feishu channel

`LarkChannel` with tenant_access_token caching, text send, and webhook event parser.

### v0.0.25 — Plugin foundation + web tools

`Plugin`, `PluginRegistry`, `PluginResult`. Built-in `WebFetchPlugin` (httpx + HTML strip), `WebSearchPlugin` (Tavily if keyed, DuckDuckGo HTML fallback).

### v0.0.24 — Interactive REPL

Bare `anthill` drops into a conversation loop. Auto-founds a default nation. Slash commands for `/trails`, `/identity`, `/power`, `/rate`.

### v0.0.23 — One-line installer

`curl -fsSL ... | bash` clones the repo, sets up a venv, installs the package, and drops a wrapper at `~/.local/bin/anthill`. Idempotent — re-run to upgrade.

### v0.0.22 — LLM judge

Optional Judge model scores worker output [0, 1]. Pheromones now reinforce quality, not just liveness. OFF by default (`ANTHILL_USE_JUDGE=1` to enable).

### v0.0.21 — Workflow templates

Mine recurring plan shapes from history. `anthill workflows mine/show`. Scout sees known shapes as hints.

### v0.0.20 — Fact distillation

Deterministic facts derived from history + pheromones. `facts.md` per nation. `anthill facts refresh/show`.

### v0.0.19 — Episodic semantic search

TF-IDF cosine over `history.jsonl`. Scout sees the top-k most similar past asks as worked examples.

### v0.0.18 — Snapshot / export

`anthill export` bundles a nation into `.tar.gz`. `anthill import` restores it. Manifest embedded for self-description.

### v0.0.17 — Alarm pheromone (negative signal)

Failure no longer fails-to-reinforce — it deposits alarm, which actively repels future routing.

### v0.0.16 — Cost tracking

Every executed subtask logs token counts to `usage.jsonl`. `anthill costs` shows breakdown by model, task type, citizen.

### v0.0.15 — Plan cache

Normalised request hashing memoises Scout output. Repeated requests skip the planning round-trip.

### v0.0.14 — Parallel DAG execution

Independent subtasks within a DAG wave run via `asyncio.gather`. Cross-wave ordering preserved.

### v0.0.13 — Four ages

`anthill power` now also shows Founding, Specialization, Culture, Statecraft progress with concrete completion criteria.

### v0.0.12 — National strength meter

Six-dimensional `anthill power`: vocabulary, specialists, success rate, max chain, feedback score, diversity. Capped 0-100.

### v0.0.11 — Ask history

Every `anthill ask` appends to `history.jsonl`. CLI: `history list/show/search`.

### v0.0.10 — Style learn

Rated outputs become exemplars; `anthill style learn` mines them into a suggested house style via LLM.

### v0.0.9 — Rate

`anthill rate up/down` reinforces or erodes the pheromones of the last ask's citizens.

### v0.0.8 — Resilience

Retry with citizen rotation on failure. Skip downstream subtasks when a dependency fails. `forbid` parameter on `Router.assign`.

### v0.0.7 — Statecraft (DAG executor)

Plans run as DAGs with dependency-aware context passing. Topological order, cycle detection, last-step synthesis.

### v0.0.6 — Rename to Nation

`Colony` → `Nation`, `--colony` → `--nation`, `colonies/` → `nations/`. Reframed as "user is the king, nation grows around you."

### v0.0.5 — Culture layer

`Culture` with `task_catalog` + `house_style`. Scout reuses known task types. Workers see house style.

### v0.0.4 — Scout (natural-language entry)

`anthill ask` accepts plain requests. Scout LLM decomposes into typed subtasks.

### v0.0.3 — Benchmark

`anthill bench` compares role vs pheromone routing. Mean gap across 4 seeds: +54.5 pp.

### v0.0.2 — Model dispatch

`ModelProvider` abstraction. DeepSeek and MiniMax providers. Config via env + `~/.anthill/config.toml`.

### v0.0.1 — Pheromone core

`PheromoneTrail`, `Agent`, `Router`, `Nation`. CLI: `init`, `spawn`, `run`, `trails`, `status`.
