# Anthill vs Claude CLI vs Hermes — deep comparison

> **Purpose**: figure out which **baseline gaps** Anthill should close
> relative to Anthropic's Claude CLI and the Hermes multi-model
> framework, and which **differentiating strengths** it should
> amplify. The conclusion drives the 0.1.7+ iteration plan.

> **Sources**: the Claude CLI section is based on public docs and
> observed repo behavior (last checked 2026-05). The Hermes section
> is based on what the user described in earlier sessions. Exact
> capability boundaries shift between versions; this doc focuses on
> **product-shape comparison**, not a per-feature matrix.

---

## One-line positioning

| | Positioning | Core metaphor |
|---|---|---|
| **Claude CLI** | One strong model + a strong tool system = an "agent workbench" | Arm a smart model so it works like a developer assistant |
| **Hermes** | Multi-model + multi-plugin "AI toolbox" | Give the developer a hub with pluggable AI parts |
| **Anthill** | Multi-model collaboration that "grows into a nation" | One task → multiple models split it → the nation learns you over time |

The three are **not substitutes**. Claude CLI plays in the
"deep single-model agent" lane. Hermes plays in the
"configurable multi-tool integration" lane. Anthill plays in the
"emergent multi-model collaboration" lane.

---

## Where Claude CLI is strong (and Anthill should learn)

### 1. CLI ergonomics are polished

- **Streaming output**: tokens render as they generate; the user *feels*
  fast even when total latency is unchanged
- **Multi-line input**: `"""` heredoc or Shift+Enter for natural wraps
- **Tab completion**: slash commands, file paths, model names all tab
- **Vim mode**: `prompt_toolkit`-driven mode switching + highlight
- **Status line**: customizable bottom statusbar
- **Session snapshots**: `/resume` jumps back to any historical session
- **Background tasks**: long-running jobs don't block the REPL

### 2. Files / code as a first-class citizen

- `@filename.py` attaches a whole file to the context
- glob patterns like `@src/**/*.py` pull a set of files
- The project directory is understood as the "working context",
  not just a search domain
- Git integration: auto-aware of staged diffs, branches, etc.

### 3. Skill / slash-command extension ecosystem

- Users author **plain markdown files** to define new slash commands
- A hooks system lets plugins intercept lifecycle (pre-ask /
  post-ask / on-error)
- MCP wires the CLI into the broader desktop AI tool ecosystem

### 4. Computer Use / Tool Use closed loop

- Browser, files, shell, images — one unified tool-calling protocol
- The Claude family is heavily trained for tool use, so calls are
  reliable in practice

---

## Where Hermes is strong (parity status for Anthill)

### Things Anthill already has (no need to belabor)

- ✓ One-line curl install
- ✓ IM platform integration (Lark / Telegram / Slack / WeCom, daemon mode)
- ✓ Plugin system (file / web / shell / docs / browser / code_exec)
- ✓ CLI config file + secrets.toml
- ✓ MCP server + client
- ✓ Workflow / Recipe templates

### Things Anthill **deliberately does not do** (Hermes does)

- ✗ **Manual model switching** — Hermes lets the user pick a model;
  Anthill makes the router learn the choice
- ✗ **One model per task** — Hermes assigns one model per task;
  Anthill splits one task across multiple models

This is a product-philosophy gap. **Anthill's differentiation
comes specifically from refusing manual switching** — that path
is Hermes's comfort zone, but it's also its ceiling.

---

## Anthill's unique / leading differentiation (to amplify)

The following do not exist in either Claude CLI or Hermes:

| Capability | Anthill module | What the user feels |
|---|---|---|
| **Multi-model collaboration on a single ask** | scout + executor + ensemble | "Translate this and explain the choices" — translate goes to deepseek, explanation goes to minimax, automatically |
| **Pheromone-based emergent specialization** | router + pheromone | The more you use it, the better the routing; no need to tell the system "X is good at Y" |
| **Citizen lifecycle (birth / retire / reproduce)** | lifecycle + reproduction | The nation is a living organism; zombie citizens retire, strong ones reproduce variant offspring |
| **Quality-driven multi-round deliberation** | deliberate | Not "stop when the model says done", but "stop when the objective quality score crosses threshold" |
| **Open-vocabulary multi-dimensional scoring** | values + DimensionCatalog | The judge proposes dimensions like "correctness / depth / tone" itself; users can re-weight them |
| **Failure attribution + immune isolation** | failure + immune | When a model has a transient outage it gets quarantined; once recovered it's probed back in |
| **Task complexity classification** | complexity | "Hi" returns instantly; "research X" runs deliberation |
| **Clarification turn** | clarify | Vague requests prompt 1–3 questions first to avoid garbage-in |
| **Visible multi-model collaboration** | repl | The REPL shows which subtask ran on which model, so you see the collaboration happen |

---

## Anthill's obvious baseline gaps

| Gap | User pain | Difficulty |
|---|---|---|
| **No streaming output** | Long tasks freeze the screen for 5–30 s; feels hung | Medium (provider layer needs streaming) |
| **Single-line input()** | Pasting code / long text breaks | Small (switch to prompt_toolkit or multi-line mode) |
| **No Tab completion** | Slash commands / model names must be typed in full | Small (readline already supports completion; needs a hook) |
| **Files as context** | "How do I change this file?" requires manual cat | Medium (implement `@file` syntax + glob) |
| **No image input** | "Why this error?" with a screenshot doesn't work | Medium (vision provider + REPL path / drag-drop) |
| **Slow startup** | First import ~1 s (rich + click both heavy) | Medium (lazy imports) |
| **No fine-grained progress feedback** | All you see is `running...` with no detail | Small (expose internal attempt state) |

---

## Strategic choice — where to go next

There are two forks:

### Fork A: close baseline UX gaps, catch up to Claude CLI

Short-term, ship streaming / multi-line / Tab completion / `@file`
so the user's **first sit-down at the REPL** doesn't feel "crude".

Cost: 4–6 patches of engineering, all imitation work, no new
differentiation.

Benefit: keeps first-wave users long enough to see Anthill's
unique value.

### Fork B: amplify the unique differentiation

Keep digging into nation lifecycle / multi-model collaboration,
ignore baseline UX.

Cost: first-time users think "this is much rougher than Claude CLI"
and may bounce before the deeper value shows up.

Benefit: differentiation goes to 11; becomes a coherent "tool
philosophy"; long-term valuable.

### Actual best choice: alternate A and B

Concrete cadence:

- **One baseline patch (A)** → **one differentiation patch (B)**
- Baseline goal: "close the gap with Claude CLI enough not to be
  embarrassing", not "surpass it"
- Differentiation goal: "shock the users who actually look", not
  "be exhaustive"

---

## Proposed iteration path (0.1.7 → 0.1.18)

Arranged A/B alternating, all patch-level per VERSIONING:

| Version | Class | Content | Effort |
|---|---|---|---|
| **0.1.7** | A | Streaming output — provider layer supports streaming; REPL renders as tokens arrive | Medium |
| **0.1.8** | B | `@file` / `@dir/**.py` syntax — auto-attach file contents to the prompt | Medium |
| **0.1.9** | A | Multi-line input — `"""` heredoc enters multi-line mode, single-message send | Small |
| **0.1.10** | B | Editable Plan — let the user edit / skip steps after Scout proposes the plan | Medium |
| **0.1.11** | A | Tab completion — slash commands / model names / nation names | Small |
| **0.1.12** | B | Nation bound to working dir — `cd /path/project && anthill` auto-loads project context | Medium |
| **0.1.13** | A | Startup optimization — lazy import drops first-launch time from ~1 s to ~300 ms | Small |
| **0.1.14** | B | Skill auto-mining — repeatedly used ask patterns auto-suggest as recipes | Medium |
| **0.1.15** | A | Image input — `attach <path>` to send an image to a vision-capable citizen | Medium |
| **0.1.16** | B | Nation-to-nation collaboration — one nation can ask another nation for help during planning | Medium |
| **0.1.17** | A | Custom slash commands — user defines them in `~/.anthill/commands/*.md` | Medium |
| **0.1.18** | B | Collaborative review mode — switch deliberation's critic to multi-citizen voting | Medium |

12 patches, **all stay at minor=1 per VERSIONING**. In theory
0.1.18 → 0.1.19 also stays patch. Expected cadence: 1–2 months
depending on maintainer time.

### When to bump minor (0.2)

Per [VERSIONING.md](../VERSIONING.md), bumping minor requires:
- A new top-level CLI command group, **or**
- Backwards-incompatible public API / on-disk format change, **or**
- A multi-version arc wrapping up, **or**
- Explicit maintainer signoff: "this deserves a release-notes moment"

Candidate 0.2 triggers (**future**, not commitments):

- When 0.1.18 lands, the A/B alternating arc closes — could call it 0.2.0
- A "nation-to-nation protocol" standard (extension of 0.1.16) — that's a new format
- Ship a web UI / VS Code plugin / desktop app

---

## Out of scope (avoid scope creep)

Explicitly **not doing** these, to avoid re-litigating:

- ✘ **IDE plugin / VS Code extension** — Anthill is a CLI tool; UI investment is left to the community
- ✘ **Repo indexing / repo embedding** — that's Claude CLI / Cursor's lane
- ✘ **Fine-tuning** — Anthill orchestrates models, it doesn't train them
- ✘ **Native GUI desktop app** — CLI + browser dashboard is enough
- ✘ **Paid cloud service** — the project stays local and open source

---

## If you got this far

If you (the maintainer) endorse this plan:

- **Start on 0.1.7 streaming output** — class A, table stakes, removes
  the "is it hung?" feeling during long deliberations.

If you want to adjust:

- Drop a patch / swap A/B order / add a new direction.

Any change is patch-level — version cadence doesn't shift.
