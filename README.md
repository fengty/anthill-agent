# Anthill

> A colony of agents that grows into a kingdom.

---

## What this is

Anthill is an attempt to build a different kind of AI tool.

You don't tell it which agent to use. You don't define roles. You give it
your work, day after day. Over time, the colony grows: agents specialize,
preferences accumulate, a culture forms. What you end up with is not a
generic assistant. It is **a small AI nation, shaped by you, that becomes
capable of harder and harder work on your behalf.**

The mechanism is simple and ancient:

> Agents leave traces. Traces become paths. Paths become organization.

The same mechanism — pheromones, citations, footpaths, prices, customs —
is what every form of large-scale coordination in nature settles on.
Anthill brings it to agent systems.

---

## Why it's different

Every multi-agent framework today is a planned economy. A human decides:
this one is the researcher, this one is the coder, this one is the reviewer.
You ship with an org chart.

Real organizations don't work this way. A startup doesn't begin with an
org chart; the org chart emerges from who turns out to be good at what.
A jazz band doesn't write a script. A city doesn't design its
neighborhoods — they grow.

Anthill applies that pattern to agents. Specialization is **discovered, not
assigned.** Culture is **inherited, not designed.** Capability is
**accumulated, not configured.**

---

## The four ages of a colony

A colony in Anthill goes through stages that look a lot like a small
society growing up.

**1. Founding.** A handful of generic agents. No roles. No preferences. The
first tasks are handled almost randomly.

**2. Specialization.** Agents that succeed at certain task types accumulate
pheromone trails on those paths. Routing starts following the trails.
Strangers walking in would see the colony has "experts" — though no one was
ever appointed.

**3. Culture.** Preferences sink into shared memory. The colony develops a
voice — terse where you are terse, careful where you are careful. New tasks
are interpreted through the colony's accumulated taste.

**4. Statecraft.** The colony can take on tasks that no individual agent
could complete alone — research, multi-step projects, work that requires
coordinated specialization across days. It has a culture, an identity, and
the strength to act on your behalf.

The whole thing belongs to you. Another user with the same code will grow
a different colony.

---

## The claim, tested

The first thing this project exists to prove is one claim, empirically:

> Reputation-based routing produces better task completion than role-based
> routing, given enough tasks for trails to form.

Across four seeds on real DeepSeek API calls, with four agents carrying
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

## The pheromone model

```
A task arrives
  ↓
Router reads the pheromone map
  ↓
The agent with the strongest trail for this task type is selected
  ↓
The agent works
  ↓
Outcome deposits a new pheromone (or fails to, on a bad result)
  ↓
Old trails decay
  ↓
Over time the map reorganizes itself
```

No central planner. No predefined roles. Just trails getting stronger
or fading. A small exploration rate (~10%) keeps the colony from getting
stuck on early local optima — the same reason real ants occasionally
wander off-trail.

---

## Quickstart

```bash
pip install anthill-agent

export ANTHILL_DEEPSEEK_KEY="sk-..."

anthill init my-colony
anthill spawn --count 4 --colony my-colony
anthill run "Translate hello to Chinese" --type translate --colony my-colony
anthill trails --colony my-colony
```

Optional MiniMax support:

```bash
export ANTHILL_MINIMAX_KEY="..."
export ANTHILL_MINIMAX_GROUP="..."
anthill spawn --count 2 --model minimax --colony my-colony
```

After a few dozen tasks, run `anthill trails` again. You will see
clusters — agents that drifted toward what they were good at. No one
told them to.

---

## Architecture

```
~/.anthill/
└── colonies/
    └── <name>/
        ├── agents.json       # individual workers and their personas
        ├── pheromones.json   # the trail map (this is the colony's brain)
        ├── memory/           # later: shared preferences, culture
        └── identity.md       # later: who this colony has become
```

Four memory tiers, mirroring how any organization coordinates:

| Memory          | Scope                                          |
| --------------- | ---------------------------------------------- |
| Goal            | Shared, immutable — what we exist for          |
| Context         | Shared, append-only — what's happening now     |
| Private         | Per-agent — accumulated personal experience    |
| Working         | Per-task — ephemeral                           |

Pheromones are not memory. They are the **architecture between memories** —
the invisible structure that decides who reads what, who acts when.

---

## Status and roadmap

Pre-alpha. The core pheromone mechanism works, the benchmark proves the
central claim, and CLI workflows run end-to-end against real models.

What's coming, in order:

- **Scout decomposition.** One natural-language request fans out into typed
  subtasks the colony can route. You stop saying `--type explain`; the
  colony figures it out.
- **Cultural memory.** Preferences accumulate as a colony-wide layer.
  "Your colony tends to be terse" becomes a real, persisted fact that
  shapes future work.
- **Colony identity.** A readable summary of who this colony has become —
  specialties, voice, what it refuses, what it has done. The first time
  you read your own colony's identity, the metaphor stops being a metaphor.
- **Cross-task learning.** What worked on one task type feeds preferences
  on related types. Skills compound.
- **Statecraft.** Coordinated multi-step work across specialists. The
  point at which a colony can do something no single agent could.

See [`docs/why-anthill.md`](docs/why-anthill.md) for the philosophical
case for any of this being worth building.

---

## Contributing

Anthill is small and opinionated. We want it to stay small.

The first contribution worth making is **trying to break the benchmark.**
Run `anthill bench` with different parameters, agents, task distributions,
or scoring functions. Tell us what breaks the claim. That's how the
project gets stronger.

After that: pheromone algorithm improvements, model providers, and
visualization. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## License

MIT — use it, fork it, learn from it, prove it wrong.
