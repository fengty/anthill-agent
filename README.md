# Anthill

> Agents leave traces. Traces become paths. Paths become organization.

---

## A different way to think about agents

Most coordination in the world is invisible.

No one designs an ant colony. No one tells worker #4823 to become a forager. There is no org chart, no manager, no plan. And yet — paths form, roles emerge, the colony adapts, the colony repairs itself when half of it dies.

The mechanism is almost embarrassingly simple. An ant walks somewhere. If the path was worth walking, it leaves a chemical trace on the way back. Other ants are biased toward stronger traces. Traces decay over time, so old paths fade unless reinforced.

That's it. From this single rule, an entire civilization runs.

**Anthill is the same idea, for agents.**

Agents don't get roles. They get tasks. When a task goes well, a trace is left — not in chemistry, but in a reputation map. Future tasks follow the stronger traces. Specialization is not assigned. It is something the colony *grows into*.

---

## Why this matters

Every multi-agent framework today is a planned economy. A human decides: this one is the researcher, this one is the coder, this one is the reviewer. The architecture is fixed before the first task runs.

Real organizations don't work this way. A startup doesn't start with an org chart; the org chart emerges from who turns out to be good at what. A jazz band doesn't write a script; the music emerges from listening. A city doesn't design its neighborhoods; they grow.

Coordination at scale — the kind that doesn't break when one node fails, the kind that adapts when the world changes — has only ever been achieved by one mechanism in nature: **accumulated traces, biased selection, decay.**

Anthill is an attempt to give that mechanism to agents.

---

## How it works

```
A task arrives
   ↓
The router reads the pheromone map
   ↓
The agent with the strongest trail for this task type is selected
   ↓
The agent works
   ↓
The outcome deposits a new pheromone (or erodes the trail, on failure)
   ↓
Old traces decay
   ↓
Over time, the map reorganizes itself
```

No central planner. No predefined roles. Just trails getting stronger or fading.

A bit of exploration noise (~10%) keeps the colony from getting stuck in early local optima — the same reason real ants occasionally wander off-trail.

---

## Quickstart

```bash
pip install anthill-agent

anthill init my-colony
anthill spawn --count 5
anthill run "Refactor this module for clarity" --type code
anthill trails
```

After a hundred tasks, run `anthill trails` again. You will see clusters — agents that drifted toward what they were good at. No one told them to.

---

## The four memories

A colony coordinates through layered memory, the same way any organization does:

| Memory          | Scope                                 |
| --------------- | ------------------------------------- |
| Goal            | Shared, immutable — what we exist for |
| Context         | Shared, append-only — what's happening |
| Private         | Per-agent — what I've learned         |
| Working         | Per-task — what I'm doing right now   |

Pheromones are not memory. They are the *structure between memories* — the invisible architecture that decides who reads what, who acts when.

---

## A small claim

The first thing this project exists to prove is one claim, empirically:

> Reputation-based routing produces better task completion than role-based routing, given enough tasks for trails to form.

Everything else — the multi-model layer, the visualizations, the persistence — comes after.

If the claim is wrong, the project should die. If the claim is right, a lot of agent architecture needs to be rethought.

---

## Status

Pre-alpha. The core pheromone mechanism works and is tested. Model dispatch and persistence are the next milestones.

Building in public. Issues and PRs welcome — especially around the pheromone algorithm itself (decay curves, deposit strategies, exploration rates), and around benchmark design.

See [`docs/why-anthill.md`](docs/why-anthill.md) for the longer philosophical case.

---

## License

MIT — use it, fork it, learn from it, prove it wrong.
