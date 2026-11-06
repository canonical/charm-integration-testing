---
name: fix-charm-bug
description: Diagnose and fix an existing Juju charm bug, then verify the same failure scenario against the patched charm in a live deployment when feasible. Use when asked to fix a charm bug, not to develop a new charm feature.
---

# Task: fix a charm bug

## Goal

Deliver a minimal, evidence-based charm bug fix. Reproduce the baseline failure
when feasible, identify its cause from observable evidence, add focused regression
coverage, and verify the original scenario succeeds in a live sandbox deployment.
Unit tests, charm packing, and a passing deploy alone do not replace live
verification of the behavior under repair.

## Workflow

1. **Establish scope and constraints.** Identify the charm/repository, bug report,
   target revision, required substrate, and whether the user also wants a PR,
   commit, or push. Read repository and package instructions before editing.
   Inspect current source, related tests, and existing mechanisms. For shared
   APIs, resource ownership, lifecycle hooks, or backend behavior, check related
   issues and active PR diffs before changing the surface.

2. **Reproduce the published baseline when feasible.** Follow
   `reproduce-charm-bug` for an isolated live deployment of the reported
   revision. Preserve its commands and evidence so the same observable scenario
   can be repeated after the change. If baseline reproduction is blocked or
   inconclusive, continue diagnostics where useful, but label uncertainty and
   do not claim a confirmed root cause or fix.

3. **Trace the cause from evidence.** Inspect actual charm, Juju, and substrate
   state at the point of failure. Distinguish setup failures from failures in
   the operation under test. Treat orchestration exceptions as potentially
   post-mutation and inspect state before retrying or cleaning up. Do not default
   to longer timeouts, retries, or relaxed assertions; require evidence that
   they address the cause.

4. **Patch the correct layer.** Make the smallest change that fixes the source
   of the defect, preserving existing behavior and safe cleanup. Check ownership
   boundaries: a guard for a shared resource must not unintentionally prevent
   cleanup of resources owned by this charm. Extract focused domain helpers
   when that makes responsibilities and edge cases easier to test; do not
   refactor solely to imitate another change. Keep substrate-specific
   workarounds scoped to the affected substrate.

5. **Add regression coverage.** Prefer a focused test that fails on the baseline
   and passes with the fix. Cover relevant edge states, such as a missing or
   foreign ownership claim, partial cleanup, or retries, rather than only the
   happy path. Keep tests deterministic and assert observable behavior.

6. **Run targeted checks.** Use the charm repository's documented test, lint,
   type-check, build, and integration commands. Read CI and test output; do not
   infer success from test code or a deploy reaching active status. Report the
   exact commands and results.

7. **Repeat the original failure scenario in a live deployment.** When feasible,
   deploy the patched charm in the sandbox and execute the same trigger and
   success criteria as the baseline. Verify the exact patched artifact/revision
   is running. Wait for lifecycle hooks and Juju operations to settle, confirm
   workload readiness, and validate the affected endpoint or resource directly.
   For metrics/async behavior, require fresh samples or canaries, not retained
   old success. The test passes only when the full intended outcome is observed.

   If live verification is infeasible, state why and what was verified instead.
   Do not describe a unit test, charm pack, bootstrap, or deploy as a successful
   live reproduction.

8. **Review and clean up.** Self-review the diff for scope, types, API
   compatibility, architecture, comments, docs, and generated artifacts. Remove
   only temporary resources created for this run; preserve shared or
   pre-existing resources. Verify teardown rather than assuming it completed.

9. **Stop at the requested boundary.** Do not file a PR, push, force-push, or
   create a commit unless the user asks. If asked to prepare or open a PR, use
   `file-pr`; follow its authorization and template rules.

## #890 lessons

In `canonical/service-mesh#890`, a live removal failure first looked like a
timing issue. Inspecting the actual CRD deletion state and logs showed deletion
had not been requested: a missing shared CNI claim caused an early return that
skipped charm-owned cleanup. The fix preserved cleanup while limiting the
ownership guard to the shared binding. Treat this as a diagnostic example, not
a mandated design: identify the owned and shared resources in the current charm.

## Output

- Baseline reproduction status and evidence.
- Root cause, with observations distinguished from inference.
- Files changed and focused regression coverage.
- Exact checks run and results.
- Patched live-deployment result and the full success condition observed, or a
  clear statement of why live verification was not feasible.
- Cleanup status; state clearly if no PR, commit, or push was requested/performed.
