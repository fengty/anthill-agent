# Strengths to keep, strengths to borrow

> The discipline this doc enforces: **集合百家长处，不做大而全**.
>
> When a feature request comes in, check it against this list. If the
> capability isn't here, the answer is **no** by default — even if
> some mainstream framework has it.

This file is the **product canon**. It says:

1. What Anthill is uniquely good at — never compromise these.
2. What we deliberately borrow from 6 other frameworks, **one
   strength each, and only one**.
3. What we explicitly will NOT do, even though some framework does
   it well. Including the reason — so the rejection doesn't get
   re-litigated.

The roadmap (`ROADMAP.md`) is bound by this doc. If the roadmap
adds something that isn't in section 1 or 2, this doc is wrong and
needs amending first — that's the trigger for a real product
discussion, not a one-line PR.

Last reviewed: 2026-05-18

---

## 1. Anthill's own strengths (NEVER compromise)

These are the six mechanisms that make Anthill different from every
other agent framework. Refactors must preserve them. New features
must serve them.

### 1.1 Pheromone-based emergent specialization
The router learns which citizen is good at which task_type from
actual outcomes. No role assignments. No declarative `agents.yaml`.
The nation becomes itself by being used.

> *No other mainstream agent framework has this. CrewAI declares
> roles. LangGraph wires edges. AutoGen passes messages. Only
> Anthill grows the specialization out of pheromone trails.*

### 1.2 Multi-model collaboration on one ask
Scout decomposes → executor runs subtasks → each goes to whichever
citizen the router picks → results fuse. One user prompt can touch
3 different models in one ask. Hermes is the closest comparator
but routes one ask to one model.

### 1.3 Structured failure attribution
`FailureReason` enum has 9 distinct buckets (`auth` / `network` /
`timeout` / `truncated` / `rate_limit` / `policy_refusal` /
`format_error` / `model_error` / `empty_response`). Each maps to a
distinct user-facing remedy. Other frameworks bundle these into "it
errored."

### 1.4 Complexity classification (trivial / normal / complex)
A pre-Scout heuristic that short-circuits "你好" into 1 token, sends
"research X" into deliberation. Saves token spend, improves latency
on trivia. Other frameworks treat every ask as full-cost.

### 1.5 Clarify turn (ask back on ambiguity)
When the request is ambiguous and not-trivial, the nation asks 1-3
clarifying questions BEFORE planning. Hermes hints at this; Anthill
ships it as a first-class step.

### 1.6 Immune system + auto-quarantine
When a citizen / model fails N times in a window with a single
attribution (`auth` / `policy_refusal` / `network`), it's
quarantined. Recovery is probed periodically. Other frameworks let
a bad model keep getting picked.

---

## 2. What we borrow — one per source

The rule: **one strength per framework, picked because it directly
amplifies one of section 1's mechanisms**. Not "they have X and
we don't" — that's how `大而全` happens.

> **0.1.34 finding (see [`docs/experience.md`](experience.md)):**
> the entries below are correct as "things to borrow" but they
> miss the integrative pattern that ties them together. Both
> Hermes and Claude Code converged on a **unified experience
> model** — one ongoing relationship that spans turns, sessions,
> surfaces, time scales, and interruption points. The borrowings
> below serve THAT model, not the other way around. The next arc
> (see ROADMAP "connective-tissue arc") is built around closing
> the unified-experience gaps in [`docs/experience.md`](experience.md)
> §4.

### 2.1 From Claude Code: persistent file-based memory (CLAUDE.md)
**Shipped 0.1.29-34.** USER.md + MEMORY.md as the two-file memory
plane. Auto-memory captures explicit signals. Hygiene consolidates.
Why this and not other Claude Code features: this is the
**foundation of "越用越聪明"** — without persistent memory between
sessions, nothing else compounds.

**Deliberately NOT borrowed from Claude Code**:
- ~50 slash commands (we ship ~25 — fewer is fewer)
- IDE bridge with JWT auth (Anthill is CLI-first, not editor-first)
- Sub-agent spawn ("swarms") — overlaps with our citizen lifecycle
- 40-tool plugin registry — our 8 plugins cover real needs

### 2.2 From Hermes: pluggable memory backends
**Planned (0.1.38).** Memory has 8 swappable provider plugins
(`mem0` / `honcho` / `holographic` / ...). Abstract Anthill's
memory layer behind a `MemoryBackend` interface; ship the
`BuiltinFTS5Backend` as default; let the community plug their own.

**Why this**: it amplifies §1.1 by letting the community contribute
specialized memory for specialized domains without forking the
codebase.

**Deliberately NOT borrowed from Hermes**:
- 9 separate provider adapter files (one per LLM)
  → Anthill's `OpenAICompatibleProvider` already covers 8 vendors
  uniformly. Per-vendor adapters are duplication.
- 18 optional skill packs (finance / health / mlops / ...)
  → Domain specialization is what nation memory + pheromones learn.
  Pre-baked skill packs would make every nation feel the same.
- 26 top-level directories in a single repo, no `src/<pkg>` layout
  → Anthill keeps `src/anthill/` discipline.

### 2.3 From OpenAI Agents SDK: `@tool` decorator
**Planned (0.1.36).** `@function_tool def f(x: str) -> str` wraps
any Python function into a registered tool. Lowers the contribution
bar from "write a Plugin subclass + register + test" to one
decorator. Pydantic auto-infers the schema.

**Why this**: it lowers the cost of adding new capabilities by 10×.
Every plugin we don't have today is a plugin someone might write
if they could do it in 5 lines.

**Deliberately NOT borrowed from OpenAI SDK**:
- 180+ public symbols → keep our public surface small (4 names today,
  ≤20 even after the decorator work)
- `Trace` / `Span` / `agent_span()` observability — see §3.1
- `handoff()` between agents → we route via pheromone, not explicit handoff

### 2.4 From Anthropic Agent SDK: lifecycle hooks
**Planned (0.1.37).** Anthill already has callback parameters
scattered across the codebase: `on_progress` / `on_clarify` /
`on_plan` / `on_phase` / `on_token` / `on_critique_token` /
`on_round`. Consolidate these into a single `HookRegistry` so users
can subscribe declaratively (`@hook("pre_ask")`).

**Why this**: it amplifies §1.4 (complexity classification) by
letting users intercept "did this trigger deliberation?" or "did
this fail with auth?" and act on it.

**Deliberately NOT borrowed from Anthropic SDK**:
- `PermissionMode` / `CanUseTool` permission framework — overkill
  for a CLI tool; covered by Anthill's plugin opt-in model
- `ThinkingConfig` / `Adaptive` etc — Anthropic-specific tuning,
  not portable
- Session API with 10+ store types → our memory layer is already
  defined

### 2.5 From LangGraph: durable checkpoints
**Already covered** by Anthill's `inflight.py` (resume after crash)
and `history.jsonl` (append-only ground truth). Per-node
checkpoints would be a marginal upgrade — **skip for now**.

> The discipline test: this would only earn its place if a user
> reports "the nation crashed mid-deliberation and I lost the
> work." Until that ticket exists, keep `inflight.py`.

### 2.6 From CrewAI: nothing
CrewAI's `role / goal / backstory` declarative pattern is the
OPPOSITE of Anthill's emergent specialization (§1.1). Adopting it
would dilute the pheromone story. Their short/long/entity memory is
already covered by USER.md/MEMORY.md + recall.

### 2.7 From AutoGen: nothing for now
The event-driven actor model is a different paradigm. Could be
worth thinking about if Anthill grows nation-to-nation
communication (the federation arc that the explorations list
mentions). For now, our pull-based router + pheromone is simpler
and sufficient.

---

## 3. Explicit rejections (don't re-litigate)

The reason this section exists: every project gets requests like
"why don't you have X like FrameworkY?" — for items below, the
answer is "we looked, we said no, here's why."

### 3.1 Tracing / Span observability (à la OpenAI SDK)
**No** until Anthill runs in production at a customer site that
actually needs Sentry / Langfuse integration. Until then, our
`history.jsonl` + `inflight` checkpoints + Rich console output cover
the same need at 10% the complexity.

Revisit at **0.2.x or later**.

### 3.2 IDE plugin / VS Code extension
**No.** Anthill is a CLI tool. The browser dashboard (in the
explorations list) is the maximum UI investment. Claude Code's
2026 strength is its IDE bridge; ours is multi-model + lifecycle.

### 3.3 Repo embedding / code indexing
**No.** That's Claude Code / Cursor's lane. The `@file` syntax +
project context block (0.1.15 / 0.1.33) is enough for "explain
this file" use cases. Full repo embeddings would inflate the
install footprint past acceptable.

### 3.4 Fine-tuning support
**No.** Anthill is about orchestrating models, not training them.
If the user needs fine-tuning, this isn't the tool.

### 3.5 GUI desktop app
**No.** CLI + (future) browser dashboard for observability. A
native GUI is more surface than we want to maintain.

### 3.6 Native Windows in CI
**Best effort, not guaranteed.** macOS + Linux are first-class.
Windows / WSL likely works but isn't tested in CI.

### 3.7 Multi-language UI strings translation
**No** for now. English-first plus the `language` inference from
0.1.32 (Chinese-first answers when the user types in Chinese) is
the compromise. Full i18n of CLI strings would 3× the maintenance
burden.

### 3.8 50+ slash commands
**No.** Cap at ~25. Every new slash must displace an old one or
demonstrate a need not covered by an existing slash + argument.

### 3.9 18 optional-skill packs (à la Hermes)
**No.** Skill mining (0.1.17) + recipe auto-promotion (planned)
grows skills from real user behavior. Pre-baked domain packs
freeze a "what a finance user needs" guess that may not match any
actual user.

### 3.10 Declarative YAML agents (à la CrewAI)
**No.** §1.1 says specialization is emergent, not declared. YAML
agents are anti-pattern for us.

### 3.11 Paid cloud service
**No.** Local + open source. Project funding doesn't come from a
hosted SaaS.

---

## 4. How this doc updates

- Adding to **section 1**: rare — only when a new mechanism proves
  itself across multiple users and patches.
- Adding to **section 2**: each entry requires (a) which framework
  it's borrowed from, (b) which section-1 mechanism it amplifies,
  (c) what we're NOT borrowing from the same framework and why.
- Adding to **section 3**: cheap — better to make rejection
  explicit than re-discuss the same exclusion every few months.
- Removing from any section: needs at least one paragraph
  explaining what changed.

Section 2 is **capped at 6 entries** — one per major framework we
look at. If a 7th framework rises to relevance, an existing entry
must lose its slot or the new framework gets nothing.

This cap is the discipline that prevents the doc from becoming a
feature wishlist. **集合百家长处** means **每家挑一个**, not
**every framework's everything**.
