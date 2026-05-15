# Anthill

> Agents leave traces. Traces become paths. Paths become organization.

**Anthill** is a multi-agent framework where specialization emerges from experience, not assignment.

No predefined roles. No org chart. Agents leave reputation trails like ants leave pheromones — and the colony self-organizes.

---

## Why Anthill?

Most agent frameworks today work like a planned economy:

```
You define roles → agents execute roles → you tune the plan
```

Anthill works like a market — or more precisely, like an ant colony:

```
Agents handle tasks → leave pheromone trails → trails reinforce paths
→ specialization emerges → colony organizes itself
```

You don't tell an ant "you are a forager." It becomes one by repeatedly walking that path successfully. Anthill does the same with agents.

---

## How it's different from Hermes / OpenClaw

|                      | Hermes                | OpenClaw               | **Anthill**                   |
| -------------------- | --------------------- | ---------------------- | ----------------------------- |
| Focus                | Personal assistant    | Personal multi-channel | **Multi-agent organization**  |
| Specialization       | Self-evolution (solo) | Skills marketplace     | **Emergent from experience**  |
| Routing              | Predefined roles      | Predefined roles       | **Pheromone-based**           |
| Mental model         | One agent grows       | One agent reaches      | **Colony self-organizes**     |

Hermes makes one agent smarter. Anthill makes a group of agents form an organization.

---

## The Pheromone Model

Every task completion leaves a trace:

```python
agent.complete(task)
  → success_score = evaluate(result)
  → pheromone.deposit(agent_id, task_type, success_score)
  → pheromone.decay_over_time()
```

Future routing follows the strongest trails:

```python
router.assign(new_task)
  → pheromone.strongest_path(task_type)
  → returns agent with highest reinforced reputation
```

No central planner. No predefined roles. Just trails getting stronger or fading.

---

## Quickstart

```bash
pip install anthill-agent

# Initialize a colony
anthill init my-colony
cd my-colony

# Spawn workers
anthill spawn --count 5

# Give the colony a task
anthill run "Build me a function that parses CSV"

# Watch specialization emerge
anthill trails
```

---

## Architecture

```
~/.anthill/
├── colonies/
│   └── default/
│       ├── pheromones.db     # the trails
│       ├── agents/           # individual workers
│       ├── memory/
│       │   ├── shared/       # colony-wide context
│       │   └── private/      # per-agent experience
│       └── skills/           # learned procedures
└── config.toml
```

Four memory tiers, mirroring how real colonies coordinate:

| Tier            | Role                                      |
| --------------- | ----------------------------------------- |
| Goal memory     | Shared, immutable — what we're building   |
| Context memory  | Shared, append-only — current state       |
| Private memory  | Per-agent — accumulated expertise         |
| Working memory  | Per-task — ephemeral                      |

---

## Status

Pre-alpha. Building in public.

The first milestone is proving one thing: **reputation routing outperforms role routing.** Once that benchmark exists, everything else follows.

---

## Contributing

We're looking for help on:

- [ ] Pheromone decay algorithms
- [ ] Multi-model dispatch layer
- [ ] Benchmark suite (Anthill vs. role-based frameworks)
- [ ] Visualization of emergent trails

See `CONTRIBUTING.md` for setup.

---

## Philosophy

If you're curious why "ant colony" is more than a cute name, read [`docs/why-anthill.md`](docs/why-anthill.md).

Short version: human civilization scaled past 150 people because we developed shared culture — coordination without central control. Agent systems are stuck at the "tribe with a chief" stage. Anthill is an attempt to build the cultural layer.

---

## License

MIT
