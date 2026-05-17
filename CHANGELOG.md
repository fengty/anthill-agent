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

---

## v0.1.1 — Version number reset (May 2026)

In the weeks after the v0.1.0 Hermes-parity milestone, the project
went through a high-frequency iteration phase that bumped the
version through 0.2.x → 0.9.x in quick succession. Every interesting
commit was getting a minor bump; with a project expected to iterate
thousands of times that would have exhausted the number space within
a year.

[`VERSIONING.md`](VERSIONING.md) was adopted with a strict rule:
**patch by default, minor only with explicit maintainer signoff.**
The package version was reset from `0.9.1` back to `0.1.1` to align
with the new policy.

Behavior at `v0.1.1` is byte-identical to what was `v0.9.1`. All the
work between the historical `v0.1.0` milestone and this reset is
preserved in git history but rolled up into this single patch under
the new rule.

**Upgrade note for early users:** since `0.1.1 < 0.9.1`, `pip install
-U anthill-agent` will not upgrade automatically. Either run
`pip install --force-reinstall anthill-agent`, or re-run the
`curl install.sh` one-liner (which always pulls the current main).

---

## v0.1.8 — Setup hardening + REPL error visibility + English audit (May 2026)

A patch focused on the rough edges first-time users were tripping on,
plus a project-wide pass to keep all repo content in English (only the
maintainer's working chat stays in Chinese).

**Setup wizard hardening**
- Model id prompt now validates against a per-provider known-good
  list (e.g. `deepseek-chat`, `deepseek-reasoner` for DeepSeek). A
  typo like just `deepseek` triggers a "use it anyway?" confirm step
  instead of silently saving a bad config.
- Citizens-to-spawn prompt re-prompts on non-int input instead of
  silently falling back to 3 — fixes the case where typing a stray
  character would create a default nation the user did not ask for.

**REPL error visibility**
- Retry log now surfaces the underlying failure reason and a short
  excerpt of the provider's error output, so a model-not-found error
  is visible instead of three opaque "retry failed" lines.
- "Welcome back" counter now counts only asks that produced at least
  one successful outcome, not raw history entries. A nation whose
  first 12 asks all errored no longer brags "handled 12 asks".

**English audit**
- `docs/comparison.md` rewritten in English (was bilingual).
- README, ROADMAP, and the few inline comments that still had Chinese
  text cleaned up. Chinese keyword data tables in
  `core/complexity.py` and `core/failure.py` are kept — they are
  language-detection data, not project content. The product name
  WeCom (企业微信) keeps its native rendering in `channels/wecom.py`.

---

## v0.1.9 — Model id picker + refreshable catalog (May 2026)

The setup wizard no longer asks users to *type* a model id. Typing
`deepseek` when the real id is `deepseek-chat` was a recurring
foot-gun. The fix is a two-part change:

**Picker UI**
- Setup wizard and `anthill model add` now show a numbered list of
  the provider's known model ids. The default is option 1, so
  hitting Enter still works for the common case. Picking "Other"
  drops to free-text entry with a confirm-on-unknown-id step.
- Custom-endpoint provider (no known list) still degrades to plain
  free-text — picker would be empty there.

**Refreshable catalog** (`anthill model catalog refresh`)
- Talks to each configured provider's `/v1/models` endpoint and
  caches the live list at `~/.anthill/model_catalog.json`.
- The picker merges live + static, so a user who refreshed
  yesterday sees today's new ids without waiting for a package
  update. Failed providers are skipped silently — the previous
  cached entry persists.
- `anthill model catalog show [PROVIDER]` inspects the cache.

The static `known_models` tuples in `providers_meta.py` still ship
as the offline fallback. Maintainer-side refresh of those defaults
happens whenever someone runs the command and proposes a PR.

Tests: 746 passing (+12 for picker UX + catalog roundtrip + HTTP
shape parsing + degraded-mode handling).
