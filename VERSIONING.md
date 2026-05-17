# Versioning Policy

> **TL;DR — Default to patch bumps. Minor and major bumps require an
> explicit signoff in the commit message or PR description.**

## The rule

Every change, no matter how big it feels in isolation, ships as a
**patch bump** (`0.9.X` → `0.9.X+1`) unless **the maintainer explicitly
states a higher bump is warranted** in the same commit.

This is deliberately stricter than semver's defaults. A project that
expects thousands of iterations cannot bump minor every time something
interesting lands — we'd hit `v50.0.0` in three years and the version
number would mean nothing.

### What earns a minor bump (Y in `X.Y.Z`)

A `Y` bump is the maintainer's explicit signal that **one of these** is
true:

- A new top-level surface was added that users will reach for by name
  (a new CLI subcommand group, a new core module that anchors a story)
- A backward-incompatible change to a public API or on-disk format
- A multi-version arc has just completed and the cumulative effect
  deserves a milestone marker
- The maintainer wants to mark a "look here" moment for users —
  release-notes worth, social-share worth

If none of these apply, **even an architectural-feeling change is still
a patch.**

### What earns a major bump (X in `X.Y.Z`)

A `X` bump means:

- The pre-1.0 honeymoon is over (`0.x` → `1.0`)
- A genuinely breaking redesign of the core abstractions
- The maintainer has run real users / production deployments on this
  release line and the contract is stable enough to promise compat

`X` bumps are very rare. `0.x` → `1.0` happens **once**, and that's
maybe the only `X` bump for years.

## How to know which one

Author the commit. Before pushing, ask:

| Question | Answer |
|---|---|
| Did I add files / tests / fix a bug / add a small feature? | **patch** (default) |
| Did I add a new CLI group, a new core module, or break a format? | **minor (Y)** — but only if I'm signing off |
| Am I changing the meaning of an existing API in a way old code can't survive? | **major (X)** — but only at planned milestones |

When in doubt, **patch**. There's never a downside to under-bumping
during pre-1.0; you can always cut a minor later by tagging the
already-pushed patch.

## Reading the history

Looking back through git log, you can tell which bumps were principled
by reading the commit message. Bumps without an explicit "this is a
minor because..." line are exceptions from before this policy and
should not be precedent.

The intent of this policy is to keep version numbers **load-bearing**.
A user who sees `v3.7.234` should be able to read:
- v3 means a major API revision happened twice
- .7 means seven release-notes-worthy moments have happened in v3
- .234 means a lot of work happened — but nothing the maintainer
  flagged as a milestone

## Specifically: when to NOT bump minor

These all stay as patch bumps:

- ✗ Adding a new file with tests — patch
- ✗ Refactoring one module into three — patch
- ✗ Adding a new CLI option to an existing command — patch
- ✗ A "spirit of v0.7" wrap-up post — patch
- ✗ Performance improvements — patch
- ✗ Cross-platform fixes — patch
- ✗ Switching default behavior of an existing feature — patch
  (only minor if the old behavior is intentionally removed)
- ✗ A new core/X.py module that wires into existing modules — patch
- ✗ A doc rewrite — patch

These earn a minor bump (only with explicit signoff):

- ✓ `anthill federation` — a brand-new top-level command group that
  serves a story the user will reach for by name
- ✓ Changing the on-disk format of `history.jsonl` such that older
  versions can't read it (a planned format migration)
- ✓ A multi-version arc finishing — "v0.5 immune system" closing,
  if the closing release deliberately marks the milestone

## Past versions

The version history before this policy contains several minor bumps
that, under this policy, would have been patches. They're not being
rewritten. Going forward (from the next commit), the rule applies.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the rest of the contributor
workflow.
