# Benchmark: does pheromone routing actually work?

The README opens with a claim. This doc is the experiment that defends it.

## The question

> If a nation of citizens has unknown, hidden capability differences, can a
> pheromone-based router discover the right specialist for each task type —
> without being told which agent is good at what?

If yes, the result should beat a blind role assignment, which is what every
existing multi-agent framework asks you to do up front.

## Setup

**Agents.** Four agents, all running the same model (`deepseek-chat`). Two
of them carry a "terse" system prompt that biases them toward 1–3 word
answers. The other two carry a "verbose" system prompt that biases them
toward 30+ word explanations. Neither router is told which agent has
which persona.

**Tasks.** 50 prompts, 25 of type `terse` (factual one-shot questions) and
25 of type `verbose` (open-ended explanations). The pool is shuffled with
a fixed seed for reproducibility.

**Scoring.** Mechanical, no LLM-judge.

- A `terse` response scores 1.0 if it contains ≤ 3 words. Otherwise 0.0.
- A `verbose` response scores 1.0 if it contains ≥ 20 words. Otherwise 0.0.

This is crude on purpose. Length-as-style is something agents can
mechanically succeed or fail at, which gives the experiment a clear signal.

## Two routing strategies, same agents and tasks

**Role routing (baseline).** At setup, each task type is bound to one agent
chosen at random. Once chosen, that agent handles every task of that type.
This models the dominant pattern today: you guess who's good at what,
write it down, and live with it.

**Pheromone routing.** All four agents start equal. The first time a task
of type T appears, the router picks a random agent. The outcome's score
deposits pheromone on the (agent, task_type) trail. Subsequent tasks of
type T are biased toward whoever has the strongest trail, with a 10 %
exploration rate. Trails decay over time.

A subtle but important detail in the router: a strength-zero trail (an
agent that tried and failed) is treated as untried. Without this, a single
failed attempt locks in the failing agent, and pheromone never recovers.

## Results

Four seeds. Same model, same tasks, same agents in each pair.

| Seed | Tasks | Role | Pheromone |   Gap |
| ---: | ---: | ---: | ---: | ----: |
|  42 |  50 | 50.0 % | 98.0 % | +48.0 |
|   1 |  40 | 50.0 % | 87.5 % | +37.5 |
|   7 |  40 |  0.0 % | 92.5 % | +92.5 |
|  99 |  40 | 50.0 % | 90.0 % | +40.0 |

Pheromone wins on every seed. The average gap is +54.5 percentage points.

The role baseline averages around 37.5 % because:

- Half the time, the random pre-assignment happens to be 50 % correct
  (one task type assigned to a matching agent, the other to a mismatched
  agent). 25 of 50 tasks pass.
- One time in four, the assignment is fully wrong. Zero pass.
- One time in four, the assignment is fully right — those runs aren't in
  this sample because of how the four seeds happened to land.

This is exactly the expected baseline for blind assignment over a small
pool, and it's the dominant case in real-world multi-agent systems.
Humans don't know the true capabilities of their agents in advance.

## What this does and does not prove

**It does prove:** when capability differences exist and are not visible
to the architect, pheromone routing can discover them and out-route a
fixed-role baseline. The mechanism is real and reproducible.

**It does not prove:** that pheromone routing wins under all conditions.
Specifically:

- If the architect already knows the right assignment, role routing
  matches pheromone routing.
- Personas in this benchmark are clean and unambiguous. Real tasks have
  overlapping skill demands.
- The success score is binary. A richer score function (correctness,
  helpfulness, time-to-completion) would tell us more.
- We don't measure how long it takes pheromone routing to converge on
  larger agent pools or with more task types. There's a regime where
  cold-start cost outweighs the routing benefit, and we haven't mapped it.

These are good follow-ups, not weaknesses of the basic claim.

## How to reproduce

```bash
# Configure your model once — written to ~/.anthill/secrets.toml.
anthill model add deepseek --provider deepseek --model deepseek-chat \
  --key sk-... --set-default

# Default benchmark — same parameters as seed 42 above
anthill bench

# Vary the task count and seed
anthill bench --terse-tasks 25 --verbose-tasks 25 --seed 7

# Tighter exploration to see what happens
anthill bench --exploration 0.05
```

Each run prints the two scores and the gap. Total cost is roughly $0.005 on
DeepSeek's pricing — about half a cent per benchmark run.

## What changes if this is wrong

If a careful replication shows the gap is noise — say, the pheromone arm
loses or ties more often than it wins — the project's central claim does
not hold, and what's left is a framework demo with no architectural
advantage. That's a real outcome and the bench command is the way to
discover it.
