# Roadmap

> **Only the "Next" section is a commitment.** Everything below it is
> "things we'd like to explore" — no order, no timeline, no promises.

## Next: 0.1.7 — streaming output

The biggest "feels slow" complaint isn't actual latency, it's the
silent wait. Provider layer adds streaming; REPL renders tokens as
they arrive. Long deliberation rounds become bearable.

Status: **planned, no code yet.**

The path beyond 0.1.7 is laid out in
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
