# Specific scenarios of generic issues

Use when a failure is a specific case of a generic issue in Charm Integration
Testing. See the
[policy](../../../docs/how-to/triage-policies.rst#specific-scenarios-of-generic-issues).

1. Find the matching generic issue, then check its existing sub-issues and
   other issues for the same scenario. The parent alone is not a duplicate.
   Follow the duplicate handling in [file-issue](../file-issue/SKILL.md) if a
   match may exist.
2. File the scenario with its exact charms, versions, environment, observed
   behavior and evidence. Explain how it relates to the parent; do not claim a
   shared cause without evidence.
3. [Link it](linking-sub-issues.md) as a sub-issue of the generic issue.
