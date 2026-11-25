Triage policies
===============

These policies decide how failed test results are tracked. Use
:doc:`issue-categories` to frame and label issues in this repository.

Known-bad revisions
-------------------

A charm revision is known bad when it contains a charm bug that has been fixed
in newer charm source or a newer revision. Do not triage other failures in
results that test or deploy a known-bad revision, even if that revision is
still the latest on a channel.

Track each known-bad revision set as a sub-issue of
`#235 <https://github.com/canonical/charm-integration-testing/issues/235>`_.
The sub-issue explains why the revisions are obsolete: the exact affected
revisions and channels, the original bug, the fix, and whether a fixed
revision is published. When a new revision is published, its test results
return to the normal triage queue.

This is a revision-based triage decision, not a claim that every failure has
the same cause. Preserve the original results and match exact revisions only;
do not infer revision ranges.

A revision is not known bad only because its channel is old, because it
failed, or because it has an open bug without a fix.

Specific scenarios of generic issues
------------------------------------

When a failure is a specific case of a generic issue in Charm Integration
Testing, track the case as a sub-issue of the generic issue. The sub-issue
records the scenario's exact charms, versions, environment and evidence. Do not
claim a shared cause with the parent without evidence.

The generic parent is not a duplicate of a distinct scenario. Check its
existing sub-issues before filing a new one.
