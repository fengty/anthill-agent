# Values

A short note on what Anthill is for and what it is not.

---

## Vendor-neutral

Anthill connects to whichever models you have keys for. We bundle
adapters for DeepSeek, MiniMax, Anthropic, OpenAI, and any
OpenAI-compatible endpoint. We do not promote one as "the right one."
The whole point of pheromone routing is that the nation discovers the
best fit for your specific workloads.

If a new provider matters, write an adapter. The interface is one
async method.

## Locale-neutral

Anthill does not favour any region, language, or audience. Plugin
descriptions, prompt examples, and documentation describe capabilities,
not geographies. CJK tokenisation, Latin tokenisation, and right-to-left
scripts all flow through the same paths.

If a user's workflow is dominated by one language, the nation will learn
that preference through normal pheromone reinforcement and culture
sinking. No language is privileged in the code.

## User-owned

Every nation lives on the user's machine. Pheromones, history, culture,
exemplars, plan cache — all of it is plain JSON / Markdown under
`~/.anthill/nations/<name>/`. Snapshot, export, fork, delete, share. The
project does not ship a hosted backend and has no plans to.

API keys are stored in plaintext under `~/.anthill/secrets.toml` with
file permissions tightened to `0600`. Encryption-at-rest is on the
roadmap; explicit consent for anything that leaves the user's machine
is non-negotiable.

## Mechanism over hype

The project's central claim is one sentence:

> Reputation-based routing produces better task completion than
> role-based routing, given enough tasks for trails to form.

`anthill bench` exists so any user can run that experiment themselves
and either confirm or falsify the claim. We do not ship a benchmark
that runs only in our hands.

If a published claim turns out to be wrong, the right response is to
update the claim in the repo and move on. We aim to be the project
that says "we were wrong about this, here is what we found."

## What we do not do

- We do not auto-share, telemeter, or phone home.
- We do not advise on what model is "best" — that is a benchmark, not
  a slogan.
- We do not market on geography, language, or community membership.
- We do not write "built for X" or "designed for Y region" anywhere
  in the code or docs.

The README and source code follow these rules. PRs that drift away from
them are not personal — they will be asked to come back.
