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

---

## v0.1.13 — Editable Plan (May 2026)

Scout's plan is no longer opaque-and-immediate. With plan review on,
the user sees the proposed subtasks first and can skip / keep / cancel
before any model burns tokens.

**The hook**
- `Nation.ask(on_plan=...)` — async callback that receives the Plan
  and returns either a (possibly modified) Plan or None to cancel.
- Fires only on real Scout output. Bypassed on cache hits,
  trivial-fast classification, `pre_plan=` (recipes), and `resume=`
  (already-locked plans).
- Cancellation surfaces as `AskResult(cancelled_by_user=True,
  outcomes=[], final_output="")`. The REPL skips history / cost
  bookkeeping for cancelled asks — nothing dirtied.

**REPL UI**
- Numbered subtask list with task_type, depends_on, and a prompt
  preview.
- Enter = run as-is. `s 2,3` = skip those indices. `k 1` = keep
  only #1. `c` = cancel. Loops until Enter or cancel.
- `/plan` slash command toggles review on/off per session. Default
  is off so nothing surprises users who haven't opted in.

**Tests** — 792 passing (+7 in `tests/test_editable_plan.py`)
- on_plan returning the plan unchanged ⇒ runs normally
- on_plan mutating ⇒ executor honors the modified plan
- on_plan returning None ⇒ cancelled_by_user=True, no calls to run
- pre_plan / cache hit / trivial-fast / resume all skip the hook

---

## v0.1.14 — Tab completion (May 2026)

Slash commands, configured model names, nation names, and `@`-token
file paths all complete on Tab. No more full-string typing for
common navigation.

**What completes**
- `/h<Tab>` → `/help`, `/history`
- `/model <Tab>` → list of configured models + subcommands
- `/nation <Tab>` → nation names on disk
- `/rate <Tab>` → `up`, `down`
- `/plan <Tab>` → `on`, `off`
- `@<Tab>` → top-level files & directories in cwd
- `@src/<Tab>` → contents of `src/`. Directories show with trailing `/`
  so you can keep tabbing in.
- Dotfiles hidden.

**Architecture**
- `cli/completion.py` splits the work in two:
  1. `ReplCompleter`: pure engine that takes a `CompletionContext`
     (slash list, model names, nation names, cwd) + a buffer +
     cursor and returns candidate strings. Unit-testable.
  2. `install_readline_completion()`: readline glue. Lazily rebuilds
     the context on every Tab so freshly-added models / nations
     show up immediately. Handles macOS libedit's different config
     syntax. Removes `@` and `/` from the default delimiters so the
     completer sees full tokens.

**Tests** — 808 passing (+16 in `tests/test_completion.py` covering
prefix matching, mid-cursor behavior, dir traversal, dotfile hiding,
and the "no completion outside slash/at-token" guard).

---

## v0.1.15 — Project context binding (May 2026)

When the REPL launches inside a project directory, Scout now knows
about it. No more typing `the file structure is...` — the planner
sees the project name, language, top-level files, and git status as
part of its episodic context.

**Detection**
- Walks up to 6 levels from cwd looking for project markers in
  priority order: `pyproject.toml`, `setup.py`, `Cargo.toml`,
  `go.mod`, `package.json`, `Gemfile`, `composer.json`, `pom.xml`,
  `build.gradle`, `CMakeLists.txt`, `Makefile`, then `.git`.
- More specific markers win (Python beats Git repo when both exist).

**What Scout sees**
```
[project: anthill-agent — Python (pyproject.toml)]
Top-level entries (8): src/, tests/, README.md, ...
Git: branch main · 3 modified file(s)
```

**UX**
- Splash shows `project: <name> (kind) · branch[*]` row when
  detected — `*` indicates a dirty repo.
- New `/project` slash command inspects the block.
- Listed in Tab completion.
- Caps: 25 top-level entries; git subprocess 2-second timeout each
  call; dotfiles hidden except `.github/`.

**Robustness**
- Every git call is wrapped in try/except. Missing `git` binary,
  detached HEAD, submodules, permission errors — all degrade to
  "no branch info" instead of crashing.
- `_list_top_level` swallows OSError so an unreadable directory
  returns empty rather than blowing up the splash.

**Tests** — 819 passing (+11 in `tests/test_project_context.py`)
- Marker detection at root + walking up from a subdirectory
- Priority (Python beats Git)
- Top-level sort + dotfile hiding (except `.github`)
- 25-entry cap
- Git status fallback for non-git dirs
- Render block shape
- Permission-denied iterdir handling

---

## v0.1.16 — Lazy imports / startup speedup (May 2026)

A-class baseline. `anthill --version` and `--help` no longer pay for
loading Nation, Router, Agent, the executor, the model providers, or
~50 other modules they don't actually need.

**The big win — `anthill/__init__.py`**
- PEP 562 `__getattr__` defers re-exports. `from anthill import
  __version__` now drops from ~120 ms to ~4 ms (30×). `from anthill
  import Nation` still works — Python falls through to our
  `__getattr__` on the first miss, materializes the class, and
  caches it on the module.
- `__all__` still lists the lazy names so `dir(anthill)` and tools
  that introspect modules see them.

**Per-command lazy imports in `cli/main.py`**
- Moved inside their command bodies:
  - `anthill.bench.compare` (only `anthill bench`)
  - `anthill.core.facts` (only `facts show / refresh`)
  - `anthill.core.workflows` (only `workflows show / mine`)
  - `anthill.core.power` (only `power`)
  - `anthill.core.snapshot` (only `export / import`)
  - `anthill.core.style_learner` (only `style learn`)
- Kept eager: `AnthillConfig`, `Nation`, `Console`, click — every
  command needs these.

**Measured (warm cache)**
- `anthill --version`: ~110 ms → ~88 ms
- `anthill --help`: ~100 ms → ~80 ms
- `from anthill import __version__`: ~120 ms → ~4 ms

**Tests** — 827 passing (+8 in `tests/test_lazy_imports.py`)
- Subprocess test confirms `from anthill import __version__` does
  NOT load `anthill.core.nation` — the critical guarantee.
- `anthill.Nation`, `.Agent`, `.PheromoneTrail`, `.Router` all
  materialize correctly via `__getattr__`.
- Unknown attribute raises `AttributeError` (standard contract).

---

## v0.1.17 — Skill auto-mining (May 2026)

The system *notices* when you've asked things like the current
request 3+ times and nudges you to crystallize it into a recipe.
You own the name; the system owns the detection.

**Detection — `core/skill_mining.py`**
- Set-cosine similarity over request tokens (same tokenizer the
  episodic search uses; consistency on purpose).
- Single-pass clustering: each successful past ask joins the first
  cluster it's similar to (threshold 0.6), or seeds a new one.
- Threshold for surfacing: ≥3 occurrences. Configurable per call.
- Scan limited to 100 most-recent successful entries — quadratic in
  scan_limit, but bounded enough for interactive use.
- Failed asks excluded — repeating a query that didn't work is
  not a skill.

**REPL — `cli/repl.py`**
- After each successful ask, mine clusters and check if the current
  request belongs to one. If yes, surface a one-line hint:
  `💡 you've asked things like '<snippet>…' 3 times. Run anthill
  recipe save to bake a skill.`
- `SessionStats.suggested_skill_ids` tracks which clusters we've
  already nudged about so the same hint doesn't repeat every turn.
- `/skills` (alias `/skill`) inspects the top 10 mined patterns
  with occurrence counts.
- Hint generation wrapped in try/except — mining is best-effort,
  never breaks the REPL.

**Tests** — 837 passing (+10 in `tests/test_skill_mining.py`)
- No clusters → empty list (the quiet case)
- 3 similar asks → 1 cluster of size 3
- Failed asks excluded
- min_occurrences threshold respected
- Most-recent entry is the cluster's representative
- Clusters ordered by occurrence count desc
- looks_like_new_match for the post-ask nudge gate
- Empty request doesn't seed a phantom cluster
- scan_limit caps inspection (older matches drop off)

---

## v0.1.18 — `/model rm` in REPL (May 2026)

User reported: "I configured a wrong model during testing and don't
know how to delete it." `anthill model remove NAME` (the CLI command)
already existed, but the REPL had no path to it — users had to leave
the session, run the CLI, and come back.

**REPL changes — `/model` subcommand**
- `/model` lists models numbered (1-based) with a `★` marker on the
  current default. Footer hints at the three remove styles.
- `/model use NAME-or-N` accepts either a name or a list index.
- `/model rm NAME-or-N` deletes one (asks for `y/N` confirmation
  unless `--yes` is passed).
- `/model rm` with no args walks every model interactively:
  prompt-per-model, `y` deletes, anything else keeps. Ctrl+C stops.
- Multi-arg: `/model rm broken duplicate stale` queues three.
- The default-model field auto-reassigns to the first surviving
  model when the current default is removed. Goes to `None` when
  the list empties.
- Secret cleanup: deleting a model also `remove_secret`s its API
  key from `secrets.toml` — no orphan secrets.

**Help / discovery**
- `HELP_TEXT` lists the new subcommands under [Steer].
- Tab completion: typing `/model <Tab>` now offers `rm` and
  `remove` alongside the existing verbs.

**Tests** — 847 passing (+10 in `tests/test_model_remove_repl.py`)
- Remove by name with confirm
- Remove by index
- `--yes` flag bypasses confirm
- Default model reassigns when removed
- Default goes to None when last model deleted
- `n` answer keeps the model
- Interactive walk handles per-model y/N
- Unknown name is a quiet no-op
- `/model use INDEX` accepts numeric arg
- Secret cleanup verified (no orphans)

---

## v0.1.19 — Refreshed model IDs against official docs (May 2026)

User caught it: `deepseek-chat` / `deepseek-reasoner` were stale —
DeepSeek announced retirement on 2026-07-24, and the canonical
current ids are v4-pro / v4-flash. Same story for OpenAI (GPT-4o is
gone, GPT-5.5 is the line) and Anthropic (Claude 4.7 is flagship).

**Verified against**
- DeepSeek: <https://api-docs.deepseek.com/quick_start/pricing>
- OpenAI: <https://developers.openai.com/api/docs/models/all>
- Anthropic: <https://platform.claude.com/docs/en/about-claude/models/overview>
- MiniMax: <https://platform.minimax.io/docs/release-notes/models>

**Changes — `cli/providers_meta.py`**

| Provider | Old default | New default | Notes |
|---|---|---|---|
| deepseek | `deepseek-chat` ☠️ | `deepseek-v4-pro` | Retires 2026-07-24 |
| openai | `gpt-4o-mini` ☠️ | `gpt-5.5` | GPT-4o-line dropped |
| anthropic | `claude-sonnet-4-5` (legacy) | `claude-opus-4-7` | 4.7 is current flagship |
| minimax | `MiniMax-M2-Stable` (gone) | `MiniMax-M2.7` | M2 line consolidated |

Retired ids (deepseek-chat / reasoner, gpt-4o-*, o1-*, claude-3-5-*,
MiniMax-M2-Stable, abab6.5s-chat) **removed** from `known_models`
tuples — per user request "被淘汰的就别记录了". Recent legacy that
the docs still mark active (claude-opus-4-6 / sonnet-4-5 / 4-1) is
kept so users on stable releases aren't forced to migrate.

**Pricing — `core/costs.py`**
- Full pricing refresh against each provider's official rate card.
- Old ids retained in the dict as best-effort cost lookup for
  history rows from before this patch — calling them now will fail,
  but `anthill costs` still renders correct numbers for past runs.

**Tests** — 847 passing.
- `test_deepseek_preset_has_known_models` now guards against
  retired ids creeping back into the allow-list (`deepseek-chat`
  and `deepseek-reasoner` must NOT be present).
- `test_model_catalog.*` updated to reference v4-pro / v4-flash.

---

## v0.1.20 — Mainstream provider lineup + SOCKS proxy fix (May 2026)

Two threads in one patch: expand the provider menu to cover the
mainstream lineup, and fix a real-user crash where SOCKS-proxy
users saw every ask fail with an opaque `(unknown)` error.

### Added providers

All wired via OpenAI-compatible endpoints; the existing
`OpenAICompatibleProvider` handles them transparently:

| Provider | Default model | Base URL | Source |
|---|---|---|---|
| google | `gemini-3.1-pro-preview` | `generativelanguage.googleapis.com/v1beta/openai` | ai.google.dev |
| xai | `grok-4.3` | `api.x.ai/v1` | docs.x.ai |
| moonshot | `kimi-k2.6` | `api.moonshot.ai/v1` | platform.moonshot.ai |
| qwen | `qwen3-max` | `dashscope.aliyuncs.com/compatible-mode/v1` | alibabacloud.com |
| zhipu | `glm-5` | `open.bigmodel.cn/api/paas/v4` | docs.z.ai |

Each provider:
- Added to `PROVIDER_PRESETS` with `known_models` containing every
  currently active id (retired snapshots omitted).
- Base URL wired into both `cli/model_catalog._PROVIDER_BASE_URLS`
  (so `anthill model catalog refresh` works) and
  `cli/model_cmd._probe_model` (so `anthill model test` works).
- Pricing rows in `core/costs._DEFAULT_PRICES_USD` from each
  provider's published rate card.
- `models/registry._OPENAI_COMPAT_PROVIDERS` set so `get_provider`
  builds the right transport.

### SOCKS proxy fix

A user hit a frustrating bug: `ALL_PROXY=socks5://...` (shadowsocks
/ clash setup) meant httpx auto-followed the proxy, but couldn't
without the `socksio` extra. Every ask failed three times with
"(unknown)" in the retry log.

- `pyproject.toml`: `httpx>=0.27.0` → `httpx[socks]>=0.27.0`. Fresh
  installs work automatically.
- `core/failure.py`: SOCKS / generic "proxy" patterns now bucket to
  `FailureReason.NETWORK` — the retry log says `(network)` so the
  user knows the bug is in their transport setup, not the model.
- `cli/repl.py`: new `_proxy_preflight()` runs once at REPL startup.
  When `ALL_PROXY`/`HTTPS_PROXY`/etc. is `socks*://` AND `socksio`
  isn't importable, prints actionable guidance before the first
  ask burns three retries:

  ```
  ⚠ ALL_PROXY=socks5://127.0.0.1:1080 is a SOCKS proxy, but the
    'socksio' package is not installed.
    Fix one of:
    · pip install 'httpx[socks]'   (or re-run the install one-liner)
    · unset ALL_PROXY              (if you didn't mean to route through it)
  ```

**Tests** — 865 passing (+18)
- `tests/test_provider_lineup.py`: 11 tests covering preset coverage,
  default-in-allow-list, base-URL wiring, pricing-row presence,
  registry transport selection, and the "no retired snapshots"
  guard (Qwen, Grok).
- `tests/test_proxy_preflight.py`: 7 tests covering the SOCKS error
  classification, the http-proxy no-op, the SOCKS+socksio-missing
  warning, the SOCKS+socksio-present silent case, and the pyproject
  pin guard.
