<!-- Keep PRs small and focused: one functional change behind an interface, or
all callers migrated here. See CONTRIBUTING.md and the strangler-fig rule. -->

## What & why
<!-- What does this change and why? Link the issue or PROGRESS.md task. -->

## Rollback
<!-- REQUIRED: how to undo this. A config flag, or "revert this commit". -->

## Definition of done
- [ ] CI is green on Linux, macOS, and Windows.
- [ ] A test exists that fails **without** this change (or a characterization test for untested code).
- [ ] Public interfaces are unchanged for callers, or all callers are migrated in this PR.
- [ ] No file exceeds the current length ratchet (`scripts/check_file_length.py`).
- [ ] No secret enters logs or model context; security regression tests still pass.
- [ ] `PROGRESS.md` updated; any architectural choice recorded in `DECISIONS.md`.

## Notes for reviewers
<!-- Anything risky, out of scope, or deferred. Be honest about gaps. -->
