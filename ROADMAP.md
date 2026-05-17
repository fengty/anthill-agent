# Roadmap

> **Only the "Next" section is a commitment.** Everything below it is
> "things we'd like to explore" — no order, no timeline, no promises.

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

## Next: 0.1.29 — USER.md + MEMORY.md foundation (Arc M, A-class)

Two persistent plain-text files, loaded at session start, injected
into every Scout + worker system prompt, viewable + editable from
the REPL.

  - `~/.anthill/USER.md` — global user profile (your preferences,
    communication style, languages you work in, things you've
    explicitly asked the nation to remember about you)
  - `~/.anthill/nations/<name>/MEMORY.md` — per-nation knowledge
    (project facts, conventions, "this nation's job is X", lessons
    learned, what's worked and what hasn't)

Slash commands:
  - `/memory` — show MEMORY.md
  - `/memory edit` — open in `$EDITOR`
  - `/profile` — show USER.md (alias `/preferences`)
  - `/profile edit` — open in `$EDITOR`
  - `/remember <text>` — append one timestamped line to MEMORY.md
  - `/remember-me <text>` — same but to USER.md

Splash card: `📓 N memory lines · M about you` when either file
has content.

Status: **planned, no code yet.**

## Planned arc: 0.1.29 → 0.1.38 (10 patches)

### Arc M — Memory & personalization (0.1.29-0.1.34)

The whole reason for this re-plan. Each patch closes one piece of
the "the more you use it the more it knows about you" loop.

| Version | Class | What | Why |
|---|---|---|---|
| **0.1.29** | A | `USER.md` + `MEMORY.md` files + slash commands + system-prompt injection | Foundation. Without explicit files there's nothing for the user to see, edit, or trust. Mirrors Claude Code / Hermes convention. |
| **0.1.30** | A | Auto-memory: after each successful ask, a lightweight pass decides whether the turn produced a "durable lesson" worth appending. Conservative — captures recurring patterns, environment facts, declared preferences, NOT raw output dumps. Triggers visible (📝 noted). | The "agent quietly learns" thing. Without auto-memory the foundation stays empty unless the user manually `/remember`s everything. |
| **0.1.31** | A | Cross-session conversation recall via FTS5. Extends 0.1.28's in-session rolling window. `/recall <query>` finds prior asks; auto-fires when a follow-up references content beyond the window. Uses SQLite FTS5, same shape as Hermes session_search. | "Tell me about that thing we discussed last week" — answers it. |
| **0.1.32** | B | User-model inference from feedback signals. Aggregates `/rate up`/`down` history, completion stats, language detection, output-length preferences over time → derives a structured profile that auto-edits `USER.md` (with diff confirmation). | The "越来越像你" payoff. Pheromones currently shape WHICH citizen runs a task — this shapes HOW the nation talks back. |
| **0.1.33** | A | Mid-execution self-correction using USER.md preferences as the rubric. After each subtask, citizen self-checks "did I answer in the style this user prefers? did I cover what they actually asked?" → auto-patches before declaring done. | The Hermes "不断想办法完成任务" gap. Once we have user-profile context (0.1.32), the self-check has a real yardstick. |
| **0.1.34** | A | Memory hygiene. 200-line cap per file (mirrors Claude Code's design rule). When over-cap, agent consolidates similar lines; oldest get archived to `MEMORY-ARCHIVE.md`. `/memory consolidate` triggers manually. Weekly check on session start. | Without this, MEMORY.md grows to 5K lines and stops being useful. |

### Arc V — Visible evolution (0.1.35-0.1.36)

Once memory works, surface the OTHER half of the learning loop —
pheromones — so the user feels both layers compounding.

| Version | Class | What | Why |
|---|---|---|---|
| **0.1.35** | B | Pheromone delta card after each ask. `+0.18 ant-3a4b on research · -0.05 ant-9c2d on review`. Specialist emergence callouts: "🐜 ant-3a4b is now your strongest researcher (was #3)". `/trails diff` for "since last week". | Solves "感受不到进化" — pheromones run silently today. |
| **0.1.36** | B | `/iterate <ask>` forced multi-round mode + rotating critic perspectives. Even when judge is happy, force depth: completeness → originality → counter-arguments → user-bias. | On-demand depth knob, complements auto-self-correction (0.1.33). |

### Arc D — Outstanding A/B roadmap closeout (0.1.37-0.1.38)

| Version | Class | What | Why |
|---|---|---|---|
| **0.1.37** | A | Image input. `attach <path>` for screenshots, vision-capable citizen routing. | Was 0.1.18 in original 12-patch arc; the only A-class baseline still missing. |
| **0.1.38** | A | Custom slash commands via `~/.anthill/commands/*.md` + builtin `commands.d/`. Inherits the Claude Code / Hermes file-as-command convention. | Power-user extensibility without forking. |

Nation-to-nation collaboration moves to the explorations list —
it's substantial enough that it should anchor a future 0.2.0
discussion rather than fit inside a 10-patch arc.

### After 0.1.38 — candidate 0.2.0

Per [VERSIONING.md](VERSIONING.md), a minor bump needs explicit
maintainer signoff. The natural trigger: 0.1.34 introduces a new
on-disk format (USER.md / MEMORY.md / MEMORY-ARCHIVE.md / FTS5
session search index). Once 0.1.38 lands, the Arc M+V+D wrap is
complete and the file format has stabilized — candidate moment.
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
