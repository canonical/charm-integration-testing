# Known-bad revision

Use when a charm revision has a bug that is fixed in newer source or a newer
revision. See the
[policy](../../../docs/how-to/triage-policies.rst#known-bad-revisions).

## Verify

1. Find the original bug report and the merged fix.
2. Verify which exact revisions lack the fix, for example by inspecting the
   published charm. Do not assume every older revision is affected.
3. Check whether a fixed revision is published, and on which channels. A merged
   fix alone does not make a fixed revision available.

If the bug is not fixed yet, the revision is not known bad; continue normal
triage.

## Track

Reuse an existing sub-issue of #235 if it covers the revisions. Otherwise,
create one and [link it](linking-sub-issues.md). Include:

- the charm, exact affected revisions and channels;
- the original bug and fix links;
- the evidence that those revisions lack the fix;
- whether a fixed revision is published;
- example test results.

## Classify results

Attach failed results for the exact revisions, whether the charm is the tested
artefact or a supporting charm. Do not require a matching failure signature,
but do not include unverified revision ranges or fixed revisions. Results for
new revisions return to normal triage.
