# Contributing to Anthill

The framework is opinionated. Your nation should not be.

What we keep small is the codebase — the mechanics that every nation
runs on. What we want to grow without bound is the nation itself. So
contributions that simplify or strengthen the mechanism are welcome;
contributions that bloat the framework to anticipate every nation's
shape are not.

## Setup

```bash
git clone https://github.com/fengty/anthill-agent.git
cd anthill-agent
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run tests:

```bash
pytest
```

Type-check and lint:

```bash
mypy src/
ruff check src/
```

## What we want

- **Improvements to the pheromone algorithm.** Decay curves, deposit
  strategies, exploration vs exploitation tradeoffs. Cite a paper if
  you can.
- **Benchmarks.** The project rests on a single claim: pheromone routing
  outperforms role routing. We need reproducible benchmarks to prove
  or disprove it under different conditions.
- **Scout improvements.** Better task decomposition, smarter vocabulary
  reuse, handling failure paths cleanly.
- **Model providers.** Connecting `ModelProvider` to new APIs (Claude,
  OpenAI, local inference, hosted endpoints).
- **Visualisations.** Watching pheromone trails strengthen and fade is
  the most compelling demo we can build.
- **Statecraft.** Multi-step, dependency-aware execution for tasks that
  no single citizen can complete alone.

## What we don't want (yet)

- Web dashboards.
- Hosted SaaS layer.
- Skill marketplace.
- Plugin system.

These may come. For now, we're proving fundamentals — emergent
specialization, vocabulary stability, culture as a layer.

## Style

- Type hints on everything public.
- Comments only when the *why* isn't obvious.
- New features go in their own module first; only fold into `core/`
  after the second concrete caller.

## Filing issues

Before opening an issue, please check:

1. Is this about the core mechanism, or peripheral tooling? Core gets
   priority.
2. Can you reproduce it with a minimal example?
3. Does it conflict with the "framework stays small, nations grow
   unbounded" philosophy?

## Versioning

**Default to patch bumps.** Anthill expects thousands of iterations
across its lifetime; bumping minor for every interesting commit would
exhaust the number space within a year. Minor and major bumps are
reserved for moments the maintainer explicitly flags as milestones.

Full policy + examples: [`VERSIONING.md`](VERSIONING.md).

In one sentence: **patch by default, minor only with maintainer signoff
written into the commit message.**

## Philosophy

Read [`docs/why-anthill.md`](docs/why-anthill.md). If you disagree with
the philosophy, that's fine — but please raise it as an issue before
submitting a large PR.
