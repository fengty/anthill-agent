# Roadmap

> **Only the "Next" section is a commitment.** Everything below it is
> "things we'd like to explore" — no order, no timeline, no promises.
>
> **Bound by [`docs/strengths.md`](docs/strengths.md).** New patches
> must amplify one of Anthill's section-1 mechanisms OR adopt one of
> the section-2 borrowed strengths. Anything outside that gets pushed
> to "Explorations" or rejected. **集合百家长处，不做大而全**.

## Direction: "越用越聪明，越来越像你"

The single biggest gap surfaced by real-user testing in May 2026:
Anthill has all the substrate for learning (pheromones, history,
episodic search, recipes, skill mining) but **none of it is
persistent + visible + editable across sessions**. Pheromones learn
silently. History exists but isn't distilled. Nothing tells the
user "here's what I learned about you."

Meanwhile Claude Code (v2.1.138, May 2026) and Hermes (Nous Research)
have **converged on the same two-file memory pattern**: a
user-global profile (`~/.claude/CLAUDE.md` / `~/.hermes/memories/USER.md`)
plus a project-scoped knowledge file (`<proj>/CLAUDE.md` /
`~/.hermes/memories/MEMORY.md`), both injected into the system
prompt at session start, both editable plain text, both auto-grown
from session events.

**Anthill ships none of this today.** Fixing it is the entire focus
of 0.1.29 → 0.1.34 (Arc M — Memory).

## Next: 0.1.35 — Pheromone delta card (Arc V, amplifies §1.1)

After each ask, a one-line card shows which citizens just gained or
lost expertise on which task_types:

```
🐜 pheromone update:
  +0.18  ant-3a4b  research      (now your strongest researcher, was #3)
  +0.05  ant-9c2d  review
  -0.02  ant-7g23  translate     (failed retry)
```

Plus `/trails diff` for "since last week".

Strengths.md justification: amplifies §1.1 (pheromone-based
emergent specialization). The mechanism exists; this makes it
visible to the user per ask, which closes the reported "感受不到
进化" complaint.

Status: **planned, no code yet.**

## Disciplined arc: 0.1.35 → 0.1.40 (six patches)

Each one mapped against [`docs/strengths.md`](docs/strengths.md). No
patch enters this list unless it amplifies a §1 mechanism OR is one
of the three remaining §2 borrowings.

| Version | What | strengths.md ref |
|---|---|---|
| **0.1.35** | Pheromone delta card + `/trails diff` + specialist emergence callouts | §1.1 — make emergent specialization visible |
| **0.1.36** | `@tool def f(): ...` decorator (Plugin subclass kept for back-compat). Pydantic auto-infers the schema | §2.3 — borrowed from OpenAI Agents SDK |
| **0.1.37** | Unified `HookRegistry` consolidating `on_progress` / `on_clarify` / `on_plan` / `on_phase` / `on_token` / `on_critique_token` / `on_round` into one bus | §2.4 — borrowed from Anthropic Agent SDK |
| **0.1.38** | `MemoryBackend` interface + `BuiltinFTS5Backend` default. Community can plug mem0 / chromadb without forking | §2.2 — borrowed from Hermes |
| **0.1.39** | `/iterate <ask>` forces multi-round depth with rotating critic perspectives (completeness → originality → counter-arguments → user-bias) | §1.2 + §1.4 — multi-model collab + complexity classification |
| **0.1.40** | Mid-execution self-correction: post-subtask "did I actually answer the question?" gates declaring done, using USER.md as the rubric | §1.3 + §2.1 — failure attribution + persistent memory |

### Explicitly dropped from earlier roadmap drafts

The "10-patch Arc V+D" version of this plan had: tracing/Span,
image input, custom slash commands, declarative YAML agents,
per-node checkpoints. Per `strengths.md` §3:

- **Tracing/Span** (§3.1): revisit at 0.2.x when there's a real
  customer needing Sentry / Langfuse integration.
- **Image input** (was 0.1.37): orthogonal to §1.* mechanisms.
  Pushed to "Explorations." A user pulling Anthill into a vision
  task would convince us; today nobody is.
- **Custom slash commands** (was 0.1.38): we're at ~25 slashes
  already — §3.8 caps that.
- **Per-node checkpoints**: `inflight.py` already covers crash
  resume; §2.5 — marginal upgrade.
- **Declarative YAML agents**: §3.10 — anti-pattern for §1.1.

### After 0.1.40 — candidate 0.2.0

Natural trigger: 0.1.34 introduced new on-disk formats (USER.md /
MEMORY.md / MEMORY-ARCHIVE.md / FTS5 session search index); 0.1.38
introduces the MemoryBackend interface. After 0.1.40, the
strengths-driven arc closes — the §1 mechanisms are all visible
or surfaced, the §2 borrowings are all landed. That's the
candidate moment for the package-split (anthill-core /
anthill-memory / anthill-cli / anthill-channels) plus an
import-path migration.

**Not a commitment** — maintainer decides at the moment.

### Recently shipped (most-recent first)

- **0.1.28** — In-session conversation memory. Rolling window of
  recent (request, response) turns; `is_follow_up` heuristic;
  auto-wraps prompts with context when a follow-up is detected;
  `↳ continuing from N previous turn(s)` indicator. Closes the
  exact bug "» 最近热门电影 → » 我说的是 2026 年的 → 'what 2026
  topic?'" the user reported.
- **0.1.27** — Live deliberation visibility. `deliberate()`
  callbacks for phase events + critique token streaming.
- **0.1.26** — Truncation detection. max_tokens default 1024 → 4096;
  finish_reason / stop_reason threaded; quality capped at 0.6 on
  truncated outputs.
- **0.1.25** — `/model add` in REPL + literal-markup fix.
- **0.1.24** — Third-case auth fix; `/citizens migrate NAME`;
  `/model test NAME`; auth-failure hint.
- **0.1.23** — `/citizens migrate` uses the same logic as the
  diagnostic.
- **0.1.22** — Tightened citizen preflight (env-var fallback no
  longer masks a UserConfig gap).
- **0.1.21** — Citizen-model preflight + FailureReason.AUTH +
  `/citizens migrate`.
- **0.1.20** — Mainstream providers (google / xai / moonshot /
  qwen / zhipu) + SOCKS proxy fix.
- **0.1.19** — Refreshed model IDs against May-2026 official docs.
- **0.1.18** — `/model rm` in REPL.
- **0.1.17** — Skill auto-mining.
- **0.1.16** — Lazy top-level re-exports.
- **0.1.15** — Project context binding.
- **0.1.14** — Tab completion.
- **0.1.13** — Editable Plan.
- **0.1.12** — Multi-line input.
- **0.1.11** — `@file` / `@glob` syntax.
- **0.1.10** — Streaming output.
- **0.1.9** — Model id picker + refreshable catalog.
- **0.1.8** — Setup hardening + REPL error visibility.
- **0.1.7** — Claude CLI / Hermes deep comparison + 0.1.x roadmap.

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
- **Honcho-style dialectic user modeling** — beyond surface
  preferences, infer working style + cognitive patterns

### Observability
- **Prometheus metrics export** — let nation state feed into existing
  monitoring stacks
- **Web dashboard** — read-only Rich-style view of pheromones / health
  in a browser

### Ecosystem
- **Plugin marketplace** — a curated index of third-party plugins
- **Recipe sharing** — community-contributed recipe templates
- **Multi-language UI** — translations of the CLI strings
- **Nation-to-nation collaboration** — federation protocol; was
  scheduled for 0.1.37 but moved here since it deserves a real
  design pass

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
