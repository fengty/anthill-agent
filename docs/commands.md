# Anthill REPL — Full Command Reference

The REPL `/help` shows a curated short list. This page is the
exhaustive index, kept off the in-REPL surface so the help screen
stays scannable.

## Inspect

| Command | What it does |
|---|---|
| `/trails` | Pheromone map. Shows the strength of every model-task pairing the nation has learned. |
| `/identity` | The nation's accumulated "shape" — task-type vocabulary, citizen count, culture. |
| `/power` | National strength + per-citizen ages. |
| `/status` | One-line: model, citizens, tokens used, cost so far. |
| `/history` | Recent asks (last 10 by default). |
| `/history show <id>` | Open one past ask's full transcript. |
| `/search <query>` | Grep all session JSONL. Substring by default; `/regex/` switches to regex. |
| `/timing` | Per-task_type + per-phase latency aggregates over this session. |
| `/compress` | Manually collapse middle conversation turns (head + tail preserved). |
| `/project` | Show the project context Scout sees (cwd, git branch, top-level files). |
| `/skills` | Mining view: clusters of repeated past asks. |
| `/skill list` | Saved skills + usage stats + freshness. |
| `/skill rm <name>` | Remove a specific saved skill. |
| `/skill prune` | Bulk-remove skills idle for 14+ days. |
| `/skill save <name>` | Manually distill the last complex ask. |
| `/skill refine <name>` | Refine a saved skill via LLM when its quality has drifted vs baseline. |

## Memory

| Command | What it does |
|---|---|
| `/memory` | Show the nation's MEMORY.md (cross-ask lessons). |
| `/memory consolidate` | Dedup near-duplicates, archive overflow. |
| `/profile` (alias `/preferences`, `/prefs`) | Your global USER.md. |
| `/profile consolidate` | Same hygiene for USER.md. |
| `/profile accept [kind]` | Commit inferences the nation has noticed about you. |
| `/profile skip [kind]` | Dismiss pending inferences. |
| `/remember <text>` | Add a one-line lesson to the nation's MEMORY.md. |
| `/remember-me <text>` | Add a one-line fact about yourself to USER.md. |
| `/recall <query>` | Full-text search every past ask in this nation. |

## Steer

| Command | What it does |
|---|---|
| `/rate up` / `/rate down` | Reinforce or erode pheromones for the last answer. |
| `/model` | List configured models. |
| `/model add` | Interactive: add a new provider. |
| `/model use <name>` | Switch the default model. |
| `/model rm <name>` | Remove a model (interactive without argument). |
| `/model test <name>` | Verify a model's API key. |
| `/nation <name>` | Switch nation (creates if missing). |
| `/plan` | Toggle plan-review (preview subtasks before run). |
| `/setup` | Re-run the setup wizard. |

## Citizens

| Command | What it does |
|---|---|
| `/citizens` | List alive citizens + which models they run on. |
| `/citizens migrate` | Point every unresolvable citizen at the default. |
| `/citizens migrate <model>` | Evacuate every citizen on a broken model. |

## Sessions & Background

| Command | What it does |
|---|---|
| `/session` | Info about current + recent saved sessions. |
| `/bg ask <text>` | Fire an ask in the background; result returns here when done. |
| `/bg list` | Background jobs. |
| `/bg show <id>` | One background job's output. |

## Mid-ask

| Action | Effect |
|---|---|
| `Ctrl+C` | Pause the current ask → cancel or redirect with a new instruction. |
| Typing while ask is running | Buffered for next ask. |

## Auto-handled (no command needed)

These used to be commands; they're now triggered by the system at the
moment of need:

- **Login wall on a URL** — anthill prompts inline for credentials,
  then caches cookies for next time. (Cookies live in
  `~/.anthill/url_auth_state/<domain>.json`.)
- **Playwright not installed** — anthill offers to install when a
  URL fetch needs JS rendering.
- **Stale skills** — flagged at REPL startup with one-line nudge.
- **Skill quality drift** — surfaced after a skill-matched ask runs;
  shows `/skill refine <name>` reminder.
- **Skill auto-save** — after a successful complex ask that required
  refusal-retry, the nation saves it as a skill automatically.

## Where state lives

```
~/.anthill/
├── config.toml              user-editable
├── secrets.toml             chmod 600 — API keys, credentials
├── url_auth_state/          chmod 700 — Playwright cookie cache
│   └── <domain>.json        chmod 600 — per-domain cookies
├── nations/
│   └── <name>/
│       ├── history.jsonl    one line per ask
│       ├── recipes/         saved skills (TOML)
│       ├── inflight/        in-progress asks (resume targets)
│       └── ...
└── sessions/
    └── <session_id>.jsonl   one REPL session
```

All session / history / recipe files are human-readable. Edit them
when you want.
