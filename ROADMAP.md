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

## Next: 0.1.35 — Session as persisted JSONL (experience.md §6 #1)

The first patch of the **connective-tissue arc**. Today Anthill has
all the *parts* of a unified experience (memory, streaming, IM,
recall) but they aren't tied together. A user can't say `anthill
--resume` and continue a thread from yesterday. The conversation
window (0.1.28) only lives in Python memory; it dies when the REPL
exits.

What 0.1.35 does:
- Every turn (request + plan + outcomes + costs + clarification
  answers) appends to `~/.anthill/sessions/<session-id>.jsonl`.
- New session = new id at REPL start; idle reset policy mirrors
  Hermes (default 24h).
- `anthill --resume` lists recent sessions with date / first request
  / turn count; picker reopens the chosen session.
- `--fork-session <id>` branches.
- 0.1.28's `ConversationContext` hydrates from the session file at
  REPL start instead of starting empty.

Justification: see [`docs/experience.md`](docs/experience.md) §4
row "Resume across days" — currently ⚠️.

Status: **planned, no code yet.**

## Connective-tissue arc: 0.1.35 → 0.1.42 (eight patches)

**Rewritten 2026-05-18 based on the unified-experience audit in
[`docs/experience.md`](docs/experience.md).** Previous arc (pheromone
delta + `@tool` + HookRegistry + MemoryBackend + `/iterate` +
self-correction) was correct as features but wrong as priorities.
Those 6 are still planned; they now serve the experience arc instead
of defining it.

The eight patches below close the ❌/⚠️ rows in `experience.md` §4
in order of leverage. Each patch is **traceable to a specific gap**
in the unified-experience model that Hermes + Claude Code converged
on.

| Version | What | experience.md gap closed |
|---|---|---|
| **0.1.35** | Session JSONL + `anthill --resume` + session picker. Hydrates 0.1.28 context window from disk | ⚠️ Resume across days |
| **0.1.36** | Interrupt-and-steer: Ctrl+C in mid-ask becomes "redirect or cancel?" prompt. Typed text during streaming queues as a follow-up correction | ❌ No interrupt-and-steer |
| **0.1.37** | Tool transparency: `🔍 plugin-name(args) → ✓ 0.3s` line per plugin invocation. `/verbosity off / brief / verbose` controls | ⚠️ Plugins run silently |
| **0.1.38** | Background → delivery loop: `start_background` writes completion back to the originating surface (REPL prints when user returns; IM bot DMs the result) | ❌ Background doesn't deliver |
| **0.1.39** | Permission gradient: 3 modes (`/permissions full-ask / auto-safe / full-auto`). Plugins declare a safety class; mode controls approval | ❌ No permission gradient |
| **0.1.40** | Three-phase loop: post-execute `verify` step using USER.md / MEMORY.md as rubric. Concrete gap → auto-issue follow-up subtask | ❌ No gather/act/verify loop |
| **0.1.41** | IM ↔ REPL shared session: daemon-routed asks write to the same session JSONL the REPL uses. Same user → same context across surfaces | ⚠️ IM is a separate world |
| **0.1.42** | Context compaction: when ask exceeds N% of context budget, summarize older episodes. `/compact focus on X` lets user steer | ❌ No context compaction |

### What about the 4 patches from the earlier "disciplined arc"?

Pheromone delta visualization, `@tool` decorator, unified
HookRegistry, MemoryBackend interface — **still planned, just not
as the next arc**. They're refactor-grade improvements that don't
move the experience needle. After 0.1.42 closes the integrative
gaps, those four come back as 0.1.43–0.1.46 (the "internals
polish" arc). Then 0.2.0 with package split.

### Explicit drops (per `strengths.md` §3 + `experience.md` §7)

- **Tracing/Span observability** (§3.1) — revisit at 0.2.x
- **Image input** — orthogonal to experience model; Explorations
- **Custom slash commands** (§3.8) — capped at 25
- **Per-node checkpoints** — `inflight.py` already covers it
- **Declarative YAML agents** (§3.10) — anti-pattern for §1.1
- **IDE plugin** (§3.2) — Anthill is CLI-first

### After 0.1.46 — candidate 0.2.0

By then the experience is coherent + the internals are clean. That's
the package-split moment. **Not a commitment.**

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
