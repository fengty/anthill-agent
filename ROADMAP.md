# Roadmap

> **Only the "Next" section is a commitment.** Everything below it is
> "things we'd like to explore" — no order, no timeline, no promises.

## Next: v0.8 — federated experience packs

One nation can export its accumulated trail / workflow / recipe data
as a shareable bundle. Another nation can import it after verifying
the v0.7.0 hash chain and the v0.7.1 sender provenance.

Closes the loop:
- `experience export <nation>` produces a signed pack
- `experience import <pack>` runs through immune system before merging
- Imported trails carry an `origin` field, so the auditor can see where
  borrowed knowledge came from

Status: **design done, no code yet.**

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
