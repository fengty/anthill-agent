# The complete user-experience flow

> **The point of this doc**: Anthill has been built feature-by-feature.
> Hermes and Claude Code were built **experience-by-experience**.
> That's a fundamentally different design discipline.
>
> A user doesn't care that we have streaming, memory, IM channels,
> and interrupt support as 4 separate features. They care that those
> 4 things work as ONE experience — "I have an ongoing relationship
> with this agent."
>
> This doc audits Anthill against that unified experience standard.

Last reviewed: 2026-05-18

---

## 1. What "one experience" actually means

The shape Hermes and Claude Code converged on:

**The user has ONE ongoing relationship with the agent. That
relationship spans:**

| Dimension | Example |
|---|---|
| **Multiple turns** | "Make this concise" → agent edits → "good, now in Chinese" |
| **Multiple sessions** | Quit at midnight; resume next morning; continue exactly where we left off |
| **Multiple surfaces** | Start in CLI; check on it from Telegram while at lunch; finish via web UI |
| **Multiple time scales** | Instant reply for "what's 2+2"; background task that delivers a report in 3 hours |
| **Multiple interruption points** | User says "wait, not like that" mid-stream; agent stops + adjusts |

For the agent to maintain that relationship, it has to expose:

| Requirement | What it provides |
|---|---|
| **Visible thinking** | User trusts the agent isn't stuck |
| **Tool transparency** | User sees `🔍 web_search` / `💻 ls -la` as they happen, not after |
| **Permission boundaries** | Clear what agent will / won't do without asking |
| **Persistent memory** | The relationship deepens — agent knows the user better next time |

This is what makes the difference between "feels like a chatbot"
and "feels like working with a colleague."

---

## 2. How Hermes does it (verified from docs)

### One gateway, all surfaces
> *"Telegram, Discord, Slack, WhatsApp, Signal, and CLI — all from
> a single gateway process. Adding Discord or Slack later doesn't
> require a separate service; the gateway handles all platforms
> from one process."* — Hermes docs

The same agent talks to you everywhere. Memory is shared (per-chat
session store, plus user-global MEMORY.md / USER.md).

### Tool progress as a first-class UI element
> *"Tool Progress Notifications" — `💻 ls -la...🔍 web_search...📄
> web_extract...🐍 execute_code...`*

Each tool the agent uses surfaces visually. Configurable via
`display.tool_progress` (off / new / all / verbose).

### Interrupt by typing
> *"Send any message while the agent is working to interrupt it.
> In-progress terminal commands are killed immediately (SIGTERM,
> then SIGKILL after 1s) and tool calls are cancelled. Multiple
> messages sent during interruption are combined into one follow-up
> prompt."*

User keeps the floor at all times.

### Background tasks return to the same thread
> *"Background tasks on messaging platforms are fire-and-forget —
> you don't need to wait or check on them. Results arrive in the
> same chat automatically when the task finishes."*

This is the magic. `/background check all servers and report` runs
asynchronously; the result drops back into the conversation when
done — same chat, same context.

### Sessions persist with explicit reset policies
> *"Sessions persist across messages until they reset. The agent
> remembers your conversation context. Sessions reset based on
> configurable policies: daily at a specific hour or after idle
> minutes (default 1440 minutes)."*

24 hours of idle = automatic fresh session; otherwise carries
forward. Predictable.

---

## 3. How Claude Code does it (verified from docs)

### The agentic loop = gather → act → verify, repeat
> *"When you give Claude a task, it works through three phases:
> gather context, take action, and verify results. These phases
> blend together. Claude uses tools throughout."*

Not one-shot. Not "plan-then-execute." The loop keeps cycling
until the task is genuinely done.

### Tools are first-class with categories
**5 categories**: file operations / search / execution / web /
code intelligence. Plus orchestration (spawn subagents, ask
questions, etc).

Each tool call is observable and reversible.

### Permission modes are a UX gradient, not a switch
**4 modes**, cycled with `Shift+Tab`:
- **Default** — Claude asks before file edits + shell commands
- **Auto-accept edits** — edits + common filesystem commands run silently; other commands still ask
- **Plan mode** — read-only tools only; produces a plan you approve before execution
- **Auto mode** — Claude evaluates all actions with background safety checks (research preview)

This isn't a binary "approve everything / nothing" — it's a
calibrated trust gradient the user can tune in real time.

### Checkpoints make every file edit reversible
> *"Before Claude edits any file, it snapshots the current contents.
> If something goes wrong, press Esc twice to rewind to a previous
> state, or ask Claude to undo."*

Mistakes are recoverable without git.

### Sessions are first-class
> *"Each message, tool use, and result is written to a plaintext
> JSONL file under `~/.claude/projects/`. Resuming a session with
> `claude --continue` or `claude --resume` reopens it under the
> same session ID and appends new messages."*

Plus `--fork-session` to branch. Plus a session picker UI.

### Interrupt + steer is a documented pattern
> *"You can interrupt Claude at any point. If it's going down the
> wrong path, just type your correction and press Enter. Claude
> will stop what it's doing and adjust its approach based on your
> input."*

Steering is part of the agentic loop, not an emergency stop.

### Context compaction is automated
When the context window fills, Claude Code auto-compacts (clears
older tool outputs first, then summarizes the conversation).
`/context` shows what's using space. `/compact focus on X` lets
user steer compaction.

---

## 4. Anthill audit against the unified-experience standard

Honest assessment of where we are on each element:

| Element | Claude Code | Hermes | **Anthill** |
|---|---|---|---|
| Multi-turn within session | ✅ persistent JSONL | ✅ session policy | ✅ 0.1.28 rolling window |
| Resume across days | ✅ `--resume <id>` | ✅ idle-based | ⚠️ `/recall` finds old asks but can't actually CONTINUE the thread |
| Visible thinking | ✅ extended thinking toggle + thinking blocks | ✅ tool progress notifications | ✅ 0.1.27 deliberation phases (but **only** for deliberation, not plugins) |
| Tool transparency | ✅ each tool shows + reversible | ✅ progress per tool | ⚠️ plugins run silently; only the subtask result is shown |
| Interrupt + steer | ✅ press anything, Claude adjusts | ✅ send any message → SIGTERM/SIGKILL | ❌ **Ctrl+C kills the whole ask**, no steer |
| Permission modes | ✅ 4 modes via Shift+Tab | (n/a — terminal cmd approval) | ❌ binary: plugin on / off |
| Checkpoints / undo | ✅ Esc Esc | (n/a) | ⚠️ `inflight.py` can resume after CRASH but not after USER mistake |
| Same agent across surfaces | (n/a — CLI-only) | ✅ ONE gateway → 6 platforms | ⚠️ IM daemon exists (Lark/Telegram/Slack/WeCom) but **REPL and IM don't share session context** |
| Background tasks return to chat | ✅ subagents | ✅ `/background` results post back to same chat | ⚠️ `background.py` runs jobs but **doesn't push back to user** |
| Persistent memory across all surfaces | ✅ user-level CLAUDE.md | ✅ MEMORY.md + USER.md | ✅ 0.1.29 USER.md + MEMORY.md but **conversation window (0.1.28) is REPL-only** |
| Three-phase loop (gather/act/verify) | ✅ blended loop | ✅ tool-using loop | ❌ **Scout plans once, executor runs, then stops** |
| Context compaction | ✅ `/compact` + auto | ✅ context_compressor.py | ❌ none — we just truncate |

### Score: 4 ✅ / 4 ⚠️ / 4 ❌

Anthill has **parts** of the experience but not the **connective
tissue**. The four ❌s are the killers:

1. **No interrupt-and-steer** — Ctrl+C is destructive. Hermes &
   Claude Code treat interruption as a normal user move.
2. **No permission gradient** — every plugin is either ON for
   the session or OFF. No "let me see the plan first" middle.
3. **No background-task-delivery loop** — we can spawn jobs but
   the user has to come back and `anthill background show`. The
   results don't find them.
4. **No three-phase loop** — we plan once, execute, declare done.
   Claude Code's "gather → act → verify, repeat" doesn't exist
   in Anthill.

And the four ⚠️s are also real:

- `/recall` finds old asks but doesn't bring them BACK as the
  active conversation. There's no `anthill --resume <id>`.
- Plugin tool calls happen silently — the user doesn't see
  `🔍 web_search` like in Hermes.
- IM daemon is its own world — a user can't say something on
  Telegram and have it appear in their REPL session memory.
- 0.1.28's conversation context is in-process Python only;
  doesn't persist across REPL restarts.

---

## 5. What this means for strengths.md

`strengths.md` §2 currently lists 4 borrowings (Claude CLAUDE.md
memory, Hermes MemoryBackend, OpenAI `@tool`, Anthropic hooks).
**Those 4 are still right but they're solving the wrong abstraction
level.**

The right §2 entry would be:

> **From both Claude Code and Hermes — the unified experience model.**
> One ongoing relationship; multi-turn; multi-session; multi-surface;
> multi-time-scale; interrupt-and-steer; visible thinking;
> persistent memory. Not as 5 separate features — as one coherent
> design.

This isn't a single patch. It's a **design vector** that the next
~10 patches should serve. Each patch should answer: "does this
make the relationship more coherent, or does it just add another
feature?"

---

## 6. The connective-tissue arc (replaces 0.1.35-0.1.40)

If we take the experience-first framing seriously, the next arc
isn't pheromone-delta + `@tool` + HookRegistry + MemoryBackend +
`/iterate` + self-correction.

It's **the four ❌s and four ⚠️s**, in order of leverage:

| Patch | What | Closes which gap |
|---|---|---|
| **0.1.35** | Session as a persisted JSONL: `~/.anthill/sessions/<id>.jsonl`. Every turn (request + response + outcomes + costs) appends. `anthill --resume <id>` lists & reopens. 0.1.28's conversation window now hydrates from this file on startup, not from blank state | ⚠️ "Resume across days" — closes |
| **0.1.36** | Interrupt-and-steer: Ctrl+C in mid-ask stops the current subtask, asks "redirect / cancel?" instead of killing. Typed input during a streaming response is queued and applied as a follow-up after current subtask completes | ❌ "No interrupt-and-steer" — closes |
| **0.1.37** | Tool transparency: when a plugin runs, surface `🔍 plugin-name(args)` line before the call, `✓` line after with elapsed time. Configurable verbosity (off / brief / verbose) | ⚠️ "Plugins silent" — closes |
| **0.1.38** | Background → delivery: when `start_background` is invoked from REPL OR IM, completion posts back to the originating surface. Telegram bg task result drops back into the same Telegram thread | ❌ "Background doesn't deliver" — closes |
| **0.1.39** | Permission gradient: 3 modes via slash (`/permissions full-ask` / `/permissions auto-safe` / `/permissions full-auto`). Plugins declare safety class; mode controls which classes need approval | ❌ "No permission gradient" — closes |
| **0.1.40** | Three-phase loop: after the executor produces a result, an automatic "verify" step (using USER.md / MEMORY.md as rubric) decides whether to iterate. If gap is concrete → auto-issue a follow-up subtask | ❌ "No three-phase loop" — closes |
| **0.1.41** | IM ↔ REPL shared session: IM-sourced asks load + write to the same session store as the REPL. User can switch surfaces and the agent recognizes them | ⚠️ "IM is separate world" — closes |
| **0.1.42** | Context compaction: when nation.ask exceeds N% of context budget, compact older episodes summarily. `/compact focus on X` for steering | ❌ "No context compaction" — closes |

The 4 already-shipped Arc M borrowings stay valid; they're the
**memory** half of the experience. The above 8 are the
**interactivity** half.

After 0.1.42 the experience is genuinely cohesive. THAT's where
0.2.0 lives.

---

## 7. Self-criticism

I (the maintainer pair) have been adding capabilities the way an
RFC-reader adds capabilities: one section of `comparison.md` /
`strengths.md` at a time. That misses the integrative design that
both Hermes and Claude Code clearly underwent.

The user's correction — *"我用了 Claude CLI、Hermes，他们都是1个问题，
多轮对话，深度思考，im联动，这是一个完整的使用体验问题"* — is the
right level of abstraction. We were operating one level too low.

Going forward: **each new patch must trace to either §1 of
`strengths.md` (Anthill's own mechanisms) OR section 6 of this
doc (the unified experience arc).** Patches that don't earn a
place in either get bumped to the explorations list, no matter
how technically interesting.
