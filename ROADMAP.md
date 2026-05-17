# Roadmap

> **Only the "Next" section is a commitment.** Everything below it is
> "things we'd like to explore" — no order, no timeline, no promises.

## Next: 0.1.28 — Mid-execution self-correction (A-class, agentic)

The marquee item: Hermes "不断想办法更好地完成任务" mostly comes from
**mid-execution self-correction**. Anthill currently plans once,
executes, judges only at deliberation boundaries. 0.1.28 adds a
post-subtask self-assessment loop: after each subtask completes,
the citizen checks "did I actually answer what was asked?" and if
the gap is concrete (incomplete list, missed sub-question,
unaddressed constraint) auto-inserts a patch subtask.

Status: **planned, no code yet.**

## Planned arc: 0.1.28 → 0.1.37 (10 patches)

Three threads woven together — each thread directly addresses a
specific piece of accumulated user feedback. **Arc A makes Anthill
feel agentic instead of like a chatbox. Arc B makes evolution
visible. Arc C makes habit-sinking real. Arc D closes outstanding
A/B-class roadmap items.**

### Arc A — Make it feel agentic (0.1.28-0.1.30)

| Version | Class | What | Why |
|---|---|---|---|
| **0.1.28** | A | Mid-execution self-correction: post-subtask "is this complete?" auto-check; on gap, insert a patch subtask before declaring done | Solves "Hermes 不断想办法更好地完成任务" — currently Anthill plans once and stops |
| **0.1.29** | B | `/iterate <ask>` forced multi-round mode + rotating critic perspectives (completeness → originality → counter-arguments → user-bias) | Solves "需要不断 1、2、3、4 直到完成" — even when judge is happy, force depth |
| **0.1.30** | A | Follow-up mode: `» 再深入 / » 加上中文工具 / » 这里讲一下 Y` detected as continuation; auto-injects previous output + plan + critique into context as task_type="extend" | Solves "像对话框" — continuous conversation over a single thread |

### Arc B — Make evolution visible (0.1.31-0.1.32)

| Version | Class | What | Why |
|---|---|---|---|
| **0.1.31** | B | Pheromone delta card after each ask: `+0.18 ant-3a4b on research · -0.05 ant-9c2d on review`. New `/trails diff` for "since last week". Specialist-emergence callouts: "🐜 ant-3a4b is now your strongest researcher (was #3)" | Solves "感受不到进化" — pheromones run silently today |
| **0.1.32** | B | Citizen specialization profiles: each citizen auto-derives a top-3 task_type skill profile from pheromones. `/citizens detail ant-X` shows trail history. Splash card surfaces "specialists this week" | The nation becomes an organism the user can SEE, not just a backing store |

### Arc C — Habit sinking (0.1.33-0.1.34)

| Version | Class | What | Why |
|---|---|---|---|
| **0.1.33** | B | User-preference learning from `/rate up/down` over time. Nation auto-derives "user prefers concise / detailed / Chinese-first / no preamble" and pre-seeds every prompt. `/preferences` shows what's been learned | Solves "不断沉淀用户习惯" — currently /rate just deposits pheromone, doesn't shape future asks |
| **0.1.34** | B | Recipe auto-promotion. 0.1.17 mines patterns but only hints. 0.1.34 actually converts pattern → recipe after Nth match + 1 confirmation. Recipes surface at top of `/skills`. Auto-arg-extraction from divergent slots ("translate THIS to FRENCH" → recipe with `{text}` + `{target_lang}`) | Closes the loop from 0.1.17 — system notices THEN crystallizes |

### Arc D — Outstanding roadmap closeout (0.1.35-0.1.37)

| Version | Class | What | Why |
|---|---|---|---|
| **0.1.35** | A | Image input. `attach <path>` for screenshots, vision-capable citizen routing. Auto-detects vision-capable models from the catalog so the router picks right | First-class screenshot Q&A. "Why is this UI broken?" with a PNG |
| **0.1.36** | A | Custom slash commands. `~/.anthill/commands/*.md` defines new slash commands. Built-in `commands.d/` ships with sensible defaults | Power-user extensibility without needing to fork |
| **0.1.37** | B | Nation-to-nation collaboration. A nation can ask another local nation for help during planning. Cross-nation pheromone sharing (opt-in). Sets up the federation arc — candidate trigger for 0.2.0 | The "many nations" story comes online; per VERSIONING this is the kind of arc-closing milestone that might warrant 0.2.0 |

### After 0.1.37 — candidate 0.2.0

Per [VERSIONING.md](VERSIONING.md), a minor bump needs explicit
maintainer signoff. The natural candidate trigger: 0.1.37 lands
nation-to-nation, which is the first arc-completion that crosses
into "different on-disk format" (cross-nation pheromone schema).
Plus 10 patches of A/B alternating wrap. **Not a commitment** —
maintainer decides at the moment.

### Recently shipped (most-recent first)

- **0.1.27** — Live deliberation visibility. `deliberate()` gains
  `on_phase` + `on_critique_token` callbacks; REPL shows
  "🔍 critiquing (weakest dims: ...)" → critique streams inline with
  magenta ✎ gutter → "✓ critique by ant-X" → "✍ refining (round N)".
  No more 5-15s silent wait between rounds.
- **0.1.26** — Truncation detection. Bumped default max_tokens
  1024 → 4096. Threaded `finish_reason` / `stop_reason` through
  every provider. `TaskResult.truncated` field; success_score
  capped at 0.5 when truncated; `_quality_of` caps overall
  quality at 0.6 so the deliberation loop keeps going. Closes
  the "judge gave 100% to a mid-sentence answer" bug.
- **0.1.25** — `/model add` in REPL + literal-markup confirm prompt
  fix. Reuses the setup-wizard helper for the add flow.
- **0.1.24** — Third-case auth fix. When ModelEntry NAME is configured
  but its key is bad. `/citizens migrate NAME` evacuates citizens off
  the bad model. `/model test NAME` verifies the key inline.
  Runtime auth-failure counter surfaces a one-time fix-it hint after
  3 consecutive failures.
- **0.1.23** — `/citizens migrate` now uses the same logic as the
  diagnostic — single source of truth for "which citizens are broken."
- **0.1.22** — Tightened citizen preflight: env-var fallback no
  longer masks a UserConfig gap.
- **0.1.21** — Citizen-model preflight + `FailureReason.AUTH` + the
  `/citizens migrate` command.
- **0.1.20** — Mainstream provider lineup (google / xai / moonshot /
  qwen / zhipu) + SOCKS proxy fix (`httpx[socks]`).
- **0.1.19** — Refreshed model IDs against May-2026 official docs;
  retired ids removed.
- **0.1.18** — `/model rm` in REPL (numbered list, name or index, +
  interactive walk).
- **0.1.17** — Skill auto-mining. Clusters recurring asks, nudges
  "you've done this 3× — save as recipe?". `/skills` to inspect.
- **0.1.16** — Lazy top-level re-exports. `from anthill import
  __version__` from ~120 ms to ~4 ms (30× speedup).
- **0.1.15** — Project context binding. Scout sees the project name,
  kind, top-level files, and git status.
- **0.1.14** — Tab completion. Slash commands, model / nation names,
  `@`-paths.
- **0.1.13** — Editable Plan. `Nation.ask(on_plan=...)` callback +
  `/plan` toggle.
- **0.1.12** — Multi-line input via `"""` heredoc.
- **0.1.11** — `@file` / `@glob` syntax.
- **0.1.10** — Streaming output (provider SSE → REPL inline render).
- **0.1.9** — Model id picker + refreshable catalog
  (`anthill model catalog refresh`).
- **0.1.8** — Setup hardening + REPL error visibility + English audit.
- **0.1.7** — Claude CLI / Hermes deep comparison + 0.1.x roadmap.

The path beyond 0.1.37 is open — see
[`docs/comparison.md`](docs/comparison.md) for the original 12-patch
A/B arc, mostly subsumed by the threads above.

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
