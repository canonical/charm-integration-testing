---
name: triage-charm-tests
description: Triage charm integration test failures, including known-bad charm revisions and scenarios covered by generic project issues.
---

# Triage charm test failures

## Goal

Identify the failure's owner and the appropriate tracking issue without
reinvestigating revisions already known to be bad. Use
[issue categories](../../../docs/how-to/issue-categories.rst) for project
issue framing and labels. Follow [file-issue](../file-issue/SKILL.md) and
[file-as-agent](../file-as-agent/SKILL.md) when filing.

## Check for a known-bad revision first

1. Record the test result and execution links, tested charm and revision,
   channel, and relevant supporting charm revisions from execution metadata.
   Search existing issues, including the sub-issues of
   [#235](https://github.com/canonical/charm-integration-testing/issues/235),
   for those exact revisions.
2. A revision is known bad for this workflow when evidence identifies a charm
   bug in that revision and shows the bug was fixed in newer charm source or a
   newer revision. Check the original issue and merged fix, and verify which
   specific revisions contain or lack the fix. Record whether a fixed revision
   is published; a merged fix alone does not establish that it is available on
   a channel. Do not assume every older revision is affected.
3. If an execution contains a known-bad revision, do not investigate its other
   failures as separate bugs, even if it is still the latest revision on a
   channel. Reuse an existing specific sub-issue if it covers the revision;
   otherwise, create a sub-issue of #235, following the filing rules below.
   Describe the charm, exact affected revisions and channels, the original bug
   and fix links, the evidence that those revisions lack the fix, and example
   results. Distinguish a merged fix from a published fixed revision.
4. Classify results for the exact known-bad revisions, whether the charm is the
   tested artefact or a supporting charm. This is a revision-based triage
   decision, not a claim that every failure has the same cause. Preserve the
   original results; do not mark them as passed. Do not include unverified
   revision ranges or fixed revisions. New revisions and their test results
   return to normal triage; they are not automatically known bad.

If the evidence does not establish a known-bad revision, continue normal triage.
Do not treat a channel being old, a failure, or an open unfixed bug as proof
that a revision is obsolete. Use [run-charm-tests](../run-charm-tests/SKILL.md)
when a local reproduction is needed and feasible.

## Track specific scenarios

Investigate enough to distinguish a charm defect from a test framework defect,
an environment limitation, or an unknown cause. Search existing issues before
filing. Report a charm defect to its maintainers; for a generic issue in
Charm Integration Testing, create a specific scenario as a sub-issue of the
matching generic issue when it adds distinct tracking or reproduction details.
Include the scenario's exact versions, environment, observed behavior and
evidence, and explain how it relates to the parent. Do not assert a shared
cause without evidence.

An existing generic parent is not itself a duplicate of a distinct scenario.
Check its existing sub-issues and other issues for that specific scenario first;
follow the duplicate handling in `file-issue` if one may already exist.

## Filing a sub-issue

Only file or modify issues when authorized. Follow the target repository's
template, labels and disclosure rules via `file-issue`. Create the scenario
issue in `canonical/charm-integration-testing`, then link it as an actual
GitHub sub-issue, not just a mention in the body. With the new issue number and
parent number:

```bash
gh api repos/canonical/charm-integration-testing/issues/CHILD_NUMBER --jq .id
gh api --method POST repos/canonical/charm-integration-testing/issues/PARENT_NUMBER/sub_issues \
  -F sub_issue_id=CHILD_ID
gh api repos/canonical/charm-integration-testing/issues/CHILD_NUMBER/parent \
  --jq '{number, html_url}'
```

Replace the placeholders with the returned numeric issue ID and issue numbers.
Verify the parent, and report any failure to create the relationship rather than
claiming it is linked. Apply Test Observer classifications or attachment rules
only when authorized; use exact revision filters and preserve the raw results.
