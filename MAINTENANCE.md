# Maintenance Status

> **Anthill is a hobby project maintained by a single developer.**
> Iteration pace is deliberately slow and quality-first. Read this
> page before you decide whether to depend on it.

## What you can expect

| Promise | What it means |
|---|---|
| **The current `main` is always green** | 632 tests pass, ruff clean, CI passing. Every release goes through this. |
| **Reply to issues within 1 week** | Either an answer, or "I see this — busy, will look later." Silence is a bug. |
| **Reply to PRs within 2 weeks** | A real review. May not merge immediately, but you'll know where it stands. |
| **At least one commit per month** | Even if it's docs only. If you see > 6 months of silence, assume the project is in maintenance mode and ping the maintainer. |

## What you cannot expect

- **Fast feature shipping.** A new minor version is more like quarterly than weekly.
- **24/7 responses.** This isn't a job.
- **Drop-in production support.** Anthill is solid enough for personal / team use; if you're shipping it to paying users, you should be ready to fork and own maintenance.
- **Backward compat across pre-1.0 versions.** We try, but until v1.0 we reserve the right to break things in minor bumps when the design demands it.

## How to help if you want to

| Effort level | Action |
|---|---|
| 5 minutes | Open a `good-first-issue` PR (docs, tests, small fixes) |
| 1 hour | File a detailed bug report with reproduction steps |
| 1 day | Implement a new selection strategy / plugin / mutation type |
| 1 week+ | Land a feature listed in the ROADMAP, become a co-maintainer |

## Becoming a co-maintainer

If you land 3+ non-trivial PRs that get merged, the maintainer will
likely invite you to become a triager (issue/PR review rights). If you
keep contributing for a few months past that, you'll be invited to
co-maintain. The project is happy to have help — solo maintenance
is the default, not the goal.

## If this project ever gets archived

It won't disappear — GitHub Archive preserves the code. But "Archive"
status means: don't depend on it for new work, fork if you need to
extend it. The current maintainer commits to keeping the project
**unarchived** as long as either of these is true:

- There's at least one commit per quarter from anyone, OR
- There's an open issue with active discussion within the last quarter

If both stop being true for 6+ months, the project moves to Archive
status with a clear note in the README.
