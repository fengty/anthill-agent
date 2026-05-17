# Roadmap

> **Only the "Next" section is a commitment.** Everything below it is
> "things we'd like to explore" — no order, no timeline, no promises.

## Next: 0.1.15 — Nation bound to working directory (B-class)

`cd /path/to/project && anthill` should auto-load the project as
context — picking up filenames, scanning the README, etc. Today the
nation is global to `~/.anthill/`.

Status: **planned, no code yet.**

### Recently shipped

- **0.1.14** — Tab completion. ReplCompleter (pure, testable) +
  readline glue. Knows slash commands, slash sub-args (model /
  nation / rate / plan), and `@`-token file paths with glob-aware
  directory traversal. macOS libedit fallback included.
- **0.1.13** — Editable Plan. `Nation.ask(on_plan=...)` callback fires
  after Scout produces a plan but before execution. REPL UI lets users
  skip / keep subtasks or cancel. `/plan` slash command toggles
  review on/off per session. Cache hits, trivial-fast, resume, and
  `pre_plan` paths bypass the hook (already-locked plans).
- **0.1.12** — Multi-line input. Type `"""` to enter heredoc mode;
  newlines inside don't auto-submit. Closes with `"""` on its own
  line or trailing a content line. Preserves indentation for paste.
- **0.1.11** — `@file` / `@glob` syntax. Tokens like `@src/foo.py` or
  `@src/**/*.py` expand to inlined file contents prepended to the
  prompt. Binary detection, per-file 100 KB cap, 500 KB total cap.
- **0.1.10** — Streaming output. Provider layer (`ModelProvider.stream()`),
  agent wiring (`on_token` callback), `ProgressEvent(kind='token')`,
  REPL inline rendering with soft-wrap.
- **0.1.9** — Model id picker + refreshable catalog
  (`anthill model catalog refresh`).
- **0.1.8** — Setup hardening (int / model-id validation) + REPL
  error visibility + English audit.
- **0.1.7** — Claude CLI / Hermes deep comparison + 0.1.x roadmap.

The path beyond is laid out in
[`docs/comparison.md`](docs/comparison.md) — 12 patches (0.1.7 → 0.1.18)
alternating between **baseline-UX-parity** with Claude CLI and
**differentiation** (multi-model collab, lifecycle, deliberation).
All patches stay within 0.1.x per [`VERSIONING.md`](VERSIONING.md).

### Older roadmap entry (superseded)

The earlier "v0.8 federated experience packs" plan is still a real
direction but has been bumped further down. The 0.1.7-0.1.18 patches
need to land first — federation depends on the baseline being
solid enough that users actually have nations worth federating.

---

## Explorations (no commitment, no order)

These are directions that make sense given the existing closed loops.
The maintainer might tackle any of them when the mood strikes;
contributor PRs in these areas are welcome.

### Routing & decisions
- **Per-task budget hints** — Scout proposes a recommended budget for
  each subtask based on workflow history
- **Cost-quality Pareto frontier** — show the user which (cost, quality)
  combinations have been observed for each task type
- **Plan caching with similarity** — hit the cache for prompts that
  TF-IDF-match a previous successful plan

### Self-improvement
- **Prompt evolution** — Scout / Judge prompts get versioned and
  occasionally A/B'd against new candidate prompts
- **Persona inference** — derive citizen personas from the trail map
  rather than user input

### Observability
- **Prometheus metrics export** — let nation state feed into existing
  monitoring stacks
- **Web dashboard** — read-only Rich-style view of pheromones / health
  in a browser

### Ecosystem
- **Plugin marketplace** — a curated index of third-party plugins
- **Recipe sharing** — community-contributed recipe templates
- **Multi-language UI** — translations of the CLI strings

---

## What's NOT planned

These have been considered and explicitly deprioritized:

- **Fine-tuning support.** Anthill is about orchestrating models, not
  training them. If you need fine-tuning, this isn't the tool.
- **GUI desktop app.** The CLI is the primary surface. A browser
  dashboard for observability is fine; a native GUI is not.
- **Windows support guaranteed.** macOS and Linux are first-class.
  Windows / WSL likely works but isn't tested in CI.

---

## How this list is maintained

The "Next" section is updated on every release. The "Explorations"
section is append-mostly — items rarely get removed, they just sit
there until someone picks them up. "What's NOT planned" is a place
where rejected proposals go so we don't re-litigate them.

See [MAINTENANCE.md](MAINTENANCE.md) for response-time commitments.
