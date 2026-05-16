<!--
Thanks for the PR. A short checklist that mirrors what we look for at
review time — feel free to delete sections that don't apply.
-->

## What this changes

<!-- One paragraph. The "why" matters more than the "what". -->

## Closed-loop check

Anthill's design principle is that every feature's data should be read
by another feature. Before merge, please answer:

- **Produces:** what new data / state does this PR create?
- **Consumed by:** which existing or new code reads it, and what
  decision changes as a result?

If the answer is "nothing reads it yet, but X will eventually", say so
explicitly — open loops are OK as a stepping stone but they should be
called out.

## Testing

- [ ] `pytest tests/` passes locally
- [ ] `ruff check src/` clean
- [ ] New tests added for the new code (if applicable)
- [ ] `bash scripts/smoke-test.sh` passes (if CLI / persistence touched)

## Backwards compatibility

- [ ] Existing tests pass without modification, OR
- [ ] Breaking changes are clearly noted + version bump is appropriate

## Notes for reviewers

<!-- Anything unusual: design tradeoffs, follow-ups deferred, etc. -->
