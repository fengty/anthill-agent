# Contributing to Anthill

Thanks for considering a contribution. Anthill is small and opinionated — we want it to stay that way.

## Setup

```bash
git clone https://github.com/anthill-agent/anthill.git
cd anthill
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

- **Improvements to the pheromone algorithm.** Decay curves, deposit strategies, exploration vs exploitation tradeoffs. Cite a paper if you can.
- **Benchmarks.** The whole project rests on the claim that pheromone routing beats role routing. We need reproducible benchmarks to prove or disprove it.
- **Model dispatch.** Connecting the `Agent.execute` method to real model APIs (Anthropic, OpenAI, local).
- **Visualizations.** Watching trails strengthen and fade is the most compelling demo we can build.

## What we don't want (yet)

- Web dashboards
- Hosted SaaS layer
- Skill marketplace
- Plugin system

These may come later. For now, we're proving one thing: emergent specialization works.

## Style

- Type hints on everything public.
- Comments only when the *why* isn't obvious.
- Keep `core/` small. New features go in their own module first.
- No abstraction without two concrete callers.

## Filing issues

Before opening an issue, please check:

1. Is this about the core pheromone mechanism, or peripheral tooling? Core gets priority.
2. Can you reproduce it with a minimal example?
3. Does it conflict with the "do one thing well" philosophy?

## Philosophy

Read [`docs/why-anthill.md`](docs/why-anthill.md). If you disagree with the philosophy, that's fine — but please raise it as an issue before submitting a large PR.
