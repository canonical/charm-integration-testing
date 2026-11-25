---
name: triage-charm-tests
description: Triage failed charm integration test results, including known-bad charm revisions and specific scenarios of generic project issues.
---

# Triage charm test failures

## Goal

Decide who owns a failure and which issue tracks it, without reinvestigating
failures already explained by a known-bad revision.

The policies are defined in
[triage policies](../../../docs/how-to/triage-policies.rst). Use
[issue categories](../../../docs/how-to/issue-categories.rst) for framing and
labels, and [file-issue](../file-issue/SKILL.md) when filing.

## Workflow

1. **Gather the facts.** Record the test result and execution links, the
   tested charm, revision and channel, and the supporting charm revisions from
   execution metadata.
2. **Check for a known-bad revision first.** Search the sub-issues of
   [#235](https://github.com/canonical/charm-integration-testing/issues/235)
   for the exact revisions. If one matches, attach the result to it and stop.
   If a charm bug in a revision is known to be fixed but not yet tracked,
   follow [known-bad-revision.md](known-bad-revision.md).
3. **Investigate.** Distinguish a charm defect, a test framework defect, an
   environment limitation, and an unknown cause. Use
   [run-charm-tests](../run-charm-tests/SKILL.md) when a local reproduction is
   needed and feasible.
4. **Track it.** Report charm defects to the charm's maintainers. For a
   specific case of a generic issue in this repository, follow
   [generic-issue-scenarios.md](generic-issue-scenarios.md).

Only file issues, link sub-issues, or change Test Observer results when
authorized. Preserve the original results; do not mark them as passed.
