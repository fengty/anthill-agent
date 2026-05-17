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

---

## v0.1.10 — Streaming output (May 2026)

The long-promised A-class baseline patch. Subtasks no longer freeze
the REPL for 5–30 seconds while the provider finishes a long answer.
Tokens render live as they arrive.

**Provider layer**
- `ModelProvider.stream()` is now part of the base contract. The
  default implementation calls `complete()` and yields a single
  terminal `StreamChunk` — so every existing provider already
  "streams", just in one chunk. Providers with native SSE override.
- `OpenAICompatibleProvider.stream()` implements real SSE for both
  OpenAI-shape (`/v1/chat/completions` with `stream: true` + final
  `[DONE]`) and Anthropic-shape (`/v1/messages` with
  `content_block_delta` + `message_stop` events). One shared SSE
  reader, dispatched on `provider_name`.
- New `StreamChunk(delta, done, input_tokens, output_tokens)`.
  Terminal chunk carries usage metrics; intermediate chunks just
  carry delta text.

**Agent / Executor**
- `Agent.execute(..., on_token=...)` calls the provider's streaming
  API when the callback is set. The accumulated text is byte-for-
  byte identical to the non-streaming path.
- `Nation.run` grew an `on_token` kwarg that forwards to the agent.
- `ProgressEvent` gained a new `kind='token'` with a `delta` field.
  The executor wraps the user's `on_progress` callback into an
  `on_token` so the REPL sees one ProgressEvent per delta with the
  right `attempt_number`.

**REPL**
- Tokens render dimly under each running subtask, gutter-prefixed
  with `┊` and soft-wrapped at ~80 chars so long single-paragraph
  outputs don't blow up the terminal width.
- State machine closes the stream cleanly when the subtask
  transitions to `attempt` / `finished` — no orphan partial lines.

**Backwards compatibility**
- `on_token` is opt-in at every layer. Callers that don't pass it
  get exactly the v0.1.9 behavior. Three pre-v0.1.10 test mocks
  were updated to accept the new kwarg (gracefully via `**_kw`).

Tests: 756 passing (+10 for stream contract, SSE parsing for both
provider shapes, agent streaming path, executor `kind='token'`
bridge, and edge cases like `on_token=None` short-circuit).

---

## v0.1.11 — `@file` / `@glob` attachments (May 2026)

Files as a first-class prompt context. Type `@src/foo.py` or
`@src/**/*.py` in the REPL (or `anthill ask`) and the matching files
are read and inlined above your request before Scout sees it.

**Tokenizer**
- `@` followed by any run of non-whitespace, non-`@` chars.
- Trailing punctuation (`,.;:!?)]}`) is trimmed so
  `look at @foo.py, then @bar.py` works.
- Tokens that look like email addresses produce a "not found"
  warning rather than a crash — the REPL renders a yellow `⚠`.

**Resolution**
- Glob metacharacters (`*?[`) trigger `pathlib.Path.glob` expansion
  against the working directory. `**` works recursively.
- Literal paths resolve relative to cwd unless absolute.
- Same file referenced twice is read once (dedup by `Path.resolve()`).

**Safety caps**
- Per-file cap: 100 KB. Larger files are skipped with a `⚠` warning.
- Total cap: 500 KB across all attachments. Once exceeded, later
  files are skipped and the block is flagged `truncated=True`.
- Binary detection: a NUL byte in the first 1 KiB ⇒ skip.
- UTF-8 decode failures fall back to `errors="replace"` so a stray
  byte doesn't blow up the whole expand.

**REPL feedback**
- On success: `📎 attached N file(s): foo.py, bar.py · X.X KB`
- On error: `⚠ skipped @missing.py (not found)` per token.
- `/help` now lists the attachment syntax under a new section.

**Implementation**
- `src/anthill/core/attachments.py`: `parse_at_tokens`,
  `expand_attachments`, `AttachmentBlock.render()`.
- `cli/repl.py`: expansion happens in `_handle_ask` before
  `nation.ask`. Visible request stays as-typed (history / plan
  cache hashing unchanged); the effective request with file
  contents inlined is what reaches Scout.
- `cli/main.py`: one-shot `anthill ask "..."` path gets the same
  treatment (including the deliberate and ensemble branches).

Tests: 773 passing (+17 covering tokenization, glob recursion,
dedup, caps, binary skip, UTF-8 fallback, rendered block shape,
absolute paths, and the prompt-prepend round trip).

---

## v0.1.12 — Multi-line input (May 2026)

Pasting a code snippet or a long prompt no longer auto-submits at
the first newline. Type `"""` to enter heredoc mode; subsequent
lines accumulate until a closing `"""`.

**Behaviors**
- Plain single-line input still submits on Enter as before.
- `"""` alone on a line opens multi-line mode; continuation prompt
  is `  ... ` (visually distinct from the normal `» `).
- Closer: `"""` alone on a line, or trailing a content line
  (`last line"""`).
- Inline form: `"""hello"""` on one line returns `hello`.
- Empty multi-line block (immediate close) returns the empty string;
  the REPL skips it like any empty input.
- EOF (Ctrl+D) inside the block submits what has been accumulated —
  handy for piped input.
- Ctrl+C inside the block bubbles up so the REPL cancels the request.
- Leading whitespace of the first content line is preserved
  (`    def foo():` survives). Trailing blank lines are stripped.
- `@file` tokens inside a multi-line block are still picked up at
  the next stage by the attachment expander.

**Implementation**
- `cli/repl.py` gains `_read_request_line()`. The main loop calls it
  in place of raw `input("» ")`.
- `/help` mentions `"""` under the [Editing] section.

Tests: 785 passing (+12 in `tests/test_multiline_input.py` covering
the inline pair, opener-with-content, trailing-closer, blank lines,
indentation preservation, EOF/Ctrl+C semantics, empty block, and
`@file` survival).
