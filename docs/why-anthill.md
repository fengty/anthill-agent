# Why Anthill

## The problem with current agent frameworks

Every multi-agent framework today asks you to define roles upfront:

```python
researcher = Agent(role="researcher", tools=[search, scrape])
coder = Agent(role="coder", tools=[file_edit, run_tests])
reviewer = Agent(role="reviewer", tools=[diff, comment])
```

This is a planned economy. You decide the structure, and the agents execute it.

It works — until you hit the limit of what one human can plan. Real organizations don't work this way. A startup doesn't start with an org chart; the org chart emerges from who turns out to be good at what.

## The colony model

Ant colonies have no architect. No queen tells worker #4823 to become a forager. Specialization emerges from a simple mechanism:

1. An ant walks a path
2. If the path leads to food, it leaves pheromones on the way back
3. Other ants are biased toward stronger pheromone trails
4. Pheromones decay over time, so old paths fade if unused

That's it. From this single rule, you get foraging patterns, optimal pathfinding, role division, and resilient self-repair when ants die.

It's the same mechanism Anthill uses for agents:

1. An agent completes a task
2. If the task succeeds, a pheromone trail is deposited (agent, task_type, strength)
3. The router biases future similar tasks toward agents with stronger trails
4. Trails decay over time, so unused expertise fades

No roles. No assignments. Just trails getting stronger or fading.

## What this gives you

**Self-organization.** Add ten generalist agents to a colony. After 100 tasks, you'll see clusters of specialization emerge — without anyone designing them.

**Resilience.** Remove an agent. The router shifts to the next-strongest trail. The colony keeps working.

**Adaptation.** Task distribution shifts? Old trails decay, new ones strengthen. The colony reorganizes itself.

**Cultural transmission.** New agents joining read existing trails. They start biased toward the colony's established patterns — they get socialized.

## The bigger picture

Human civilization scaled past the Dunbar number (~150 people) because we developed culture — shared patterns of coordination that don't require central control.

Most agent frameworks today are stuck at "tribe with a chief." A coordinator agent assigns work. It doesn't scale, and it can't evolve.

Anthill is an attempt to build the cultural layer for agent systems. Not because culture is poetic, but because it's the only known mechanism that lets independent intelligences coordinate at scale without a planner.

## What this isn't

- Not a replacement for [Hermes](https://github.com/NousResearch/hermes-agent) (personal assistant) or [OpenClaw](https://github.com/openclaw/openclaw) (multi-channel agent). Different problems.
- Not a general framework that tries to do everything. Anthill does one thing: emergent coordination via pheromone trails.
- Not magic. The agents themselves still need to be capable. The colony coordinates them — it doesn't make them smarter individually.

## Status

The first goal is to prove one claim empirically:

> **Pheromone-based routing produces better task completion rates than predefined role routing, after N tasks of warm-up.**

Once that benchmark exists and is reproducible, the rest of the framework can be built around it.
