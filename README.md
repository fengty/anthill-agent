# Anthill

[![CI](https://github.com/fengty/anthill-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/fengty/anthill-agent/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-632%20passing-brightgreen.svg)](tests/)
[![Maintenance](https://img.shields.io/badge/maintenance-hobby%20project-yellow.svg)](MAINTENANCE.md)

> Give Anthill one request. It splits the work, dispatches each piece
> to the model that does it best, and assembles the result.
> Then it remembers, and gets better next time.

**One mechanism, many models.** Anthill is the work blueprint — the
models do the work. Routing, retries, multi-dim evaluation, lifecycle,
and reproduction are all open mechanisms: who's "good," what's "good,"
and what to do with that information stay decisions for the LLMs and
the user, not the tool.

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/fengty/anthill-agent/main/scripts/install.sh | bash
```

**If `github.com` times out** (common in mainland China and some
corporate networks), use a GitHub mirror:

```bash
ANTHILL_REPO=https://kkgithub.com/fengty/anthill-agent.git \
  bash <(curl -fsSL https://raw.githubusercontent.com/fengty/anthill-agent/main/scripts/install.sh)
```

Or set `HTTPS_PROXY` to whatever proxy you already run (clash / v2ray /
corp HTTP proxy); git + pip both honor it automatically.

Then configure a model and drop into the REPL:

```bash
anthill model add deepseek \
  --provider deepseek \
  --model deepseek-chat \
  --key sk-... \
  --set-default

anthill                  # drops into an interactive REPL
```

Or run the one-shot wizard for an interactive walkthrough:

```bash
anthill setup
```

Your key is written to `~/.anthill/secrets.toml` (chmod 600, auto-added
to `.gitignore`) so you never have to re-export it. Configure more
providers with `anthill model add <name>` and list them with
`anthill model list`. No environment variables, no dotfile edits.

The installer detects Python 3.9+, clones into `~/.anthill-agent/`,
sets up an isolated venv, and drops a wrapper at `~/.local/bin/anthill`.
Re-run the installer any time to upgrade.

---

## Run with Docker

```bash
docker build -t anthill-agent .

# 1. Start the container with the state volume mounted.
docker run -d --name anthill -p 8765:8765 \
  -v anthill-state:/home/anthill/.anthill \
  anthill-agent

# 2. Configure model + channel inside the container — written into the
#    volume, so subsequent restarts inherit the config.
docker exec -it anthill anthill model add deepseek \
  --provider deepseek --model deepseek-chat --key sk-... --set-default
docker exec -it anthill anthill channel add larkbot \
  --kind lark --app-id cli_... --app-secret ...
```

The webhook server listens on port 8765, and all nation state (citizens,
pheromones, history, culture, plan cache, secrets) lives in the named
volume so restarts do not lose memory — and you only configure each
provider once.

---

## Three ways to talk to it

**1. Terminal REPL**

```bash
$ anthill
Anthill — default (3 citizens)
» Explain pheromone routing in one sentence
Pheromone routing is the router picking the executor based on each
agent's accumulated trail of past successes...
```

**2. One-shot CLI**

```bash
anthill ask "Research the top 3 open-source LLMs and write a recommendation"
```

**3. Lark / Telegram / Slack — let your nation answer from IM**

```bash
pip install 'anthill-agent[daemon]'

anthill channel add larkbot --kind lark \
  --app-id cli_... --app-secret ...
# (or --kind telegram --bot-token ..., or --kind slack --bot-token ...)

anthill serve
```

Point your bot's webhook at `http://your-host:8765/lark/webhook` and the
nation answers any message it receives. Same nation, same memory across
channels — your Lark bot and your Slack bot share one mind.

---

## What this is

No single model is best at everything. Claude reasons better than DeepSeek
on hard logic. DeepSeek is the cheapest of the major models with strong
multilingual coverage. Kimi has the largest context. GPT is the most
reliable tool caller. Gemini sees images the others miss.

Today, you have to pick one. You buy a Claude subscription and use Claude
for everything, including the things it's worst at. Or you pay six
different vendors and switch by hand.

**Anthill takes a request, splits it into the right subtasks, and lets
multiple models collaborate on it — each model handling the piece it does
best.** Research goes to the long-context specialist. Code review goes to
the reasoning specialist. Translation goes to the cheapest reliable model.
A final synthesis step pulls everything together.

You ask once. Six models work in concert. You see one answer.

And it gets better the longer you use it. Over time, the system learns
from experience: which model is actually best at what, which subtasks
your particular workflow needs, which style of answer you prefer.

The result is not a smarter assistant. It is **an AI organisation —
shaped by you, made of many models, growing the longer it runs.**

The mechanism is ancient:

> Agents leave traces. Traces become paths. Paths become organisation.

The same mechanism — pheromones, citations, footpaths, prices, customs —
is what every form of large-scale coordination in nature settles on.
Anthill brings it to AI.

---

## What ships in the box

| | |
|---|---|
| **Multi-model collaboration** | DeepSeek + MiniMax built in; any OpenAI-compatible model pluggable |
| **Plugins** | `web_fetch`, `web_search`, `file_read`, `file_write`, `file_list`, `shell` (opt-in) |
| **IM channels** | Lark/Feishu, Telegram, Slack — one daemon, three webhooks |
| **Memory** | Episodic semantic search + workflow templates + plan cache + pheromones + facts |
| **Observability** | `anthill power` (6-dim strength), `anthill costs` (token spend), `anthill history` |
| **Portability** | `anthill export` → tar.gz, `anthill import` to restore on another machine |

---

## What one request looks like

```bash
anthill ask "Research the top 3 open-source LLMs, compare their
              strengths, and write me a one-page recommendation."
```

What happens inside:

```
Scout decomposes              4 subtasks
   ↓
research        → Kimi      (2M-token context, best for source ingestion)
compare         → Claude    (most reliable reasoning across many sources)
draft           → DeepSeek  (cheap, fluent multilingual writer)
polish          → GPT       (best at constraint following: format, length)
   ↓
Outputs chain together via dependency-aware context passing.
   ↓
You see one final document.
```

You did not assign anyone to anything. The nation chose.

Next time you ask a similar question, it routes faster — the pheromone
trails from this run carry forward. Three months in, your nation has
quietly built up a stable preference for which model handles which kind
of subtask, calibrated to **your** workflow, not the leaderboard.

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

Other tools force a single-vendor or single-model commitment.

OpenAI Agents SDK speaks only to OpenAI. Claude's SDK speaks only to
Anthropic. OpenRouter and Portkey route across vendors, but **you write
the routing rules** — they don't learn.

LangGraph and CrewAI let you wire up multiple models, but **you decide
upfront** which agent uses which model. The decision is frozen at design
time, made by a human guessing what works.

Anthill is the only tool where:

1. **One request fans out** across multiple vendors automatically.
2. **The system learns** which model is actually best at which kind of
   subtask, based on real outcomes, not benchmarks.
3. **Routing adapts over time** — as new models get added, as old models
   drift, as your workflow changes, the assignments self-correct.

You no longer choose between Claude and DeepSeek and Kimi. You use all
of them, and the nation figures out who does what.

The same pattern that makes ant colonies discover the shortest food path
without a planner — pheromone trails, exploration, decay — is what makes
your nation discover which model wins on each kind of work in your hands.

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

anthill model add deepseek --provider deepseek --model deepseek-chat \
  --key sk-... --set-default

anthill init my-nation
anthill spawn --count 4 --nation my-nation

anthill ask "Translate 'hello world' to Japanese, then explain each word" \
        --nation my-nation

anthill trails --nation my-nation
anthill identity --nation my-nation
```

Add MiniMax as a second provider so the router can specialize:

```bash
anthill model add minimax --provider minimax --model MiniMax-M2-Stable \
  --key ... --group-id ...
anthill spawn --count 2 --model minimax --nation my-nation
```

`anthill model list` and `anthill model show <name>` confirm what's
configured (keys are masked on display).

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
