# Anthill

> You are the king. The nation grows around you.

---

## What this is

Anthill is a different kind of AI tool. You don't tell it which agent to
use. You don't define roles. You give it your work, day after day. Over
time, your nation grows: citizens specialize, vocabulary stabilizes,
a culture forms. What you end up with is not a generic assistant — it is
**a real AI organisation, shaped by you, that gets bigger and better at
serving you the longer it runs.**

The mechanism is simple and ancient:

> Agents leave traces. Traces become paths. Paths become organisation.

This is the same mechanism — pheromones, citations, footpaths, prices,
customs — that every form of large-scale coordination in nature settles
on. Anthill brings it to AI.

---

## How big can your nation get?

As big as you have work for.

The framework is small on purpose — the mechanics fit in a few hundred
lines of Python — but the nation it runs is unbounded. Start with three
citizens. Grow to thirty. Grow to three hundred. Different specialists
emerge, different task vocabularies accumulate, a distinctive house style
takes shape. Your nation after six months looks nothing like anyone
else's, even if they ran the same code.

The framework stays small so your nation does not have to.

---

## Why it's different

Every multi-agent framework today is a planned economy. A human decides:
this one is the researcher, this one is the coder, this one is the
reviewer. You ship with an org chart.

Real organisations don't work this way. A startup doesn't begin with an
org chart; the org chart emerges from who turns out to be good at what.
A jazz band doesn't write a script. A city doesn't design its
neighborhoods — they grow.

Anthill applies that pattern to agents. Specialization is **discovered,
not assigned.** Culture is **inherited, not designed.** Capability is
**accumulated, not configured.**

---

## The four ages of a nation

A nation in Anthill goes through stages that look a lot like a real
society growing up.

**1. Founding.** A handful of generic citizens. No roles. No preferences.
The first requests are handled almost randomly.

**2. Specialization.** Citizens that succeed at certain task types
accumulate pheromone trails on those paths. Routing follows the trails.
The nation has experts — though no one was ever appointed.

**3. Culture.** A shared vocabulary forms. Preferences sink into a house
style. The nation develops a voice: terse where you are terse, careful
where you are careful. Every new request is interpreted through the
nation's accumulated taste.

**4. Statecraft.** The nation can take on work that no single citizen
could complete alone — research, multi-step projects, coordinated
specialization across days. It has a culture, an identity, and the
strength to act on your behalf.

The whole nation belongs to you. Another user running the same code grows
a different nation.

---

## The claim, tested

The first thing this project exists to prove is one claim, empirically:

> Reputation-based routing produces better task completion than role-based
> routing, given enough tasks for trails to form.

Across four seeds on real DeepSeek API calls, with four citizens carrying
mixed personas and a 50-50 task mix:

| Seed | Tasks | Role routing | Pheromone routing |  Gap |
| ---: | ---: | ---: | ---: | ---: |
|  42 |  50 | 50.0% | **98.0%** | +48.0 |
|   1 |  40 | 50.0% | **87.5%** | +37.5 |
|   7 |  40 |  0.0% | **92.5%** | +92.5 |
|  99 |  40 | 50.0% | **90.0%** | +40.0 |

Mean: role 37.5%, pheromone 92.0%, gap **+54.5 percentage points**.

Reproduce it yourself:

```bash
anthill bench --terse-tasks 25 --verbose-tasks 25 --seed 42
```

See [`docs/benchmark.md`](docs/benchmark.md) for the experimental setup.

---

## Quickstart

```bash
pip install anthill-agent

export ANTHILL_DEEPSEEK_KEY="sk-..."

anthill init my-nation
anthill spawn --count 4 --nation my-nation

anthill ask "Translate 'hello world' to Chinese, then explain the difference between the two characters" \
        --nation my-nation

anthill trails --nation my-nation
anthill identity --nation my-nation
```

Optional MiniMax:

```bash
export ANTHILL_MINIMAX_KEY="..."
export ANTHILL_MINIMAX_GROUP="..."
anthill spawn --count 2 --model minimax --nation my-nation
```

After a few dozen requests, `anthill identity` will start telling you who
your nation has become.

---

## Architecture

```
~/.anthill/
└── nations/
    └── <name>/
        ├── agents.json          # citizens and their personas
        ├── pheromones.json      # the trail map — the nation's brain
        └── culture/
            ├── catalog.json     # task vocabulary the nation has built
            └── house_style.md   # the nation's voice, editable by hand
```

Four memory tiers, mirroring how any organisation coordinates:

| Memory          | Scope                                          |
| --------------- | ---------------------------------------------- |
| Goal            | Shared, immutable — what the nation exists for |
| Context         | Shared, append-only — what's happening now     |
| Private         | Per-citizen — accumulated personal experience  |
| Working         | Per-task — ephemeral                           |

Pheromones are not memory. They are the **architecture between memories** —
the invisible structure that decides who reads what, who acts when.

---

## Status and roadmap

Pre-alpha. Mechanics work, the benchmark proves the central claim, and
the CLI runs end-to-end against real models.

What's coming, in order:

- **Closed-loop culture.** Today the house style is hand-edited. Next it
  learns from user feedback — accepted vs rejected outputs reshape the
  voice automatically.
- **Statecraft.** Coordinated multi-step work across specialists. The
  point at which your nation can do something no single citizen could.
- **Difficulty escalation.** As the nation matures, it should take on
  harder tasks than it could at founding — the visible accumulation of
  national strength.
- **Cross-task learning.** What worked on one task type feeds preferences
  on related types. Expertise compounds.

See [`docs/why-anthill.md`](docs/why-anthill.md) for the philosophical
case for any of this being worth building.

---

## Contributing

The framework is opinionated. Your nation should not be.

The first contribution worth making is **trying to break the benchmark.**
Run `anthill bench` with different parameters, citizens, task
distributions, or scoring functions. Tell us what breaks the claim. That
is how the project gets stronger.

After that: pheromone algorithm refinements, model providers, scout
improvements, visualization, statecraft. See
[`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## License

MIT — use it, fork it, learn from it, prove it wrong.
