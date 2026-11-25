---
name: investigate-deployment-failures
description: Investigate, group, reproduce, and fix Test Observer deployment failures attributed to one charm, and record per-signature results in Jira. Use when asked to work a Jira ticket such as "Investigate and fix deployment failures caused by <charm>" (e.g. under epic SQT-1253), or to verify such an investigation after its fix merged.
---

# Task: investigate deployment failures caused by a charm

## Goal

Turn the `test_deploy` failures attributed to one charm X into a small set of
failure signatures, reproduce each one, fix it once at the correct layer, verify
the fix, and record results in Jira. Every signature must end fixed, tracked, or
explicitly accepted. Example: SQT-1341 (opentelemetry-collector-k8s) is the
pilot of the process defined in SQT-1340 for epic SQT-1253.

**Inputs:** the Jira key of the investigation ticket (preferred), or a charm
name. Any failure count in the ticket description is context only; the
baseline is the query below.

## Definitions

**Counted failure:** a FAILED `test_deploy` result whose execution metadata has
`failure:charm` = X, where `test_build_bundle` PASSED in the same execution.
Exclude results already linked to open `wontfix` issues in
`canonical/charm-integration-testing`; those are accepted.

- Query with `search_test_results`, using
  `template_ids=["charm_integration_testing.test_suite.test_deploy"]`,
  `test_result_statuses=["FAILED"]`, and `execution_metadata` entries of the form
  `base64(category):base64(value)`. Keys in this skill are written without the
  `charm_qa:` prefix that CI adds when posting them, but Test Observer stores
  them prefixed, so the filter category must include it. For example,
  `base64("charm_qa:failure:charm"):base64("<X>")`. An unprefixed category
  matches nothing.
- `failure:charm` is only emitted for `JujuWaitTimeoutError`
  (`record_failure_execution_metadata` in
  `charm_integration_testing/test_suite/conftest.py`). Other exceptions emit
  `failure:cli:*`, `failure:validator:interface:*`, `failure:build_bundle:*`,
  or `resource_discrepancy:*` instead.
- X being noncompliant at timeout does not prove X caused the failure; it may
  be waiting on another app. Keep the **initial label** (from metadata)
  separate from the **confirmed cause** (from evidence).

TODO: confirm whether the "typical filtering" behind the epic pass rate
excludes anything beyond `wontfix`.

**Signature (failure category):** a subset of Test Observer attachment-rule
filter fields; results matching the same filter are the same category:

- `test_case_names`: normally `test_deploy`;
- `execution_metadata`: `failure:charm: X`, plus `failure:charm:<X>:status`
  (values like `unit:<status>:<normalized message>`) or other `failure:*` keys
  only when needed to separate distinct failures;
- `environment_names`: optional.

Keep signatures attachment-rule compatible so triage can reuse them. This skill
does not create attachment rules or register issues in Test Observer; that is
triage's job.

## Workflow

0. **Intake (read-only).** Read the Jira ticket and its epic. Check related
   Jira tickets, open PRs in this repo and the charm's repo, and existing
   `static/charm-overrides/` entries for X. Do not duplicate in-flight work;
   link to it instead.

1. **Baseline.** Run the counting query with `search_test_results`, then
   intersect execution IDs with PASSED `test_build_bundle` results and drop
   accepted (`wontfix`) results. Record the exact filters (including the
   encoded metadata filters), the date run, the resulting executions, and the
   affected bundles (target/neighbor charm pairs, environments).

2. **Group into signatures.** Group by the metadata values above, then pick one
   representative execution per signature. Use `get_test_execution`,
   `download_and_extract_juju_logs`, `search_juju_logs`, and
   `read_juju_log_file` to look past `JujuWaitTimeoutError` to the app and
   status actually blocking the deployment. Record the initial label now; the
   confirmed cause is filled in later from reproduction evidence.

3. **Reproduce each representative.** No approval is needed. Follow
   `reproduce-charm-bug` rules: isolated model, settle before judging,
   collect evidence, clean up only run-owned resources. Use `run-charm-tests`
   to map the execution's parameters.
   - **Replay attempt (first):** reuse the bundle the failing execution
     deployed. CI copies `generated-target-bundle.yaml` and
     `generated-neighbor-bundle.yaml` into `logs/`, uploaded to the `charm-qa`
     Swift bucket under `<test_observer_environment>/<execution_id>`; the
     GitHub job summary also prints them, best effort. Pass them with
     `--target-bundle` / `--neighbor-bundle`
     and start from `--current-state no_controller` so `test_build_bundle`
     does not regenerate them. TODO: the logs URL
     (`charm.logs.test-observer.canonical.com`) currently returns 403 to
     `download_and_extract_juju_logs`, so there is no MCP path to the saved
     bundles yet. Do not bypass the 403. If the bundles can't be fetched, record
     that and use the current-code attempt. TODO: confirm in a live run that
     `--current-state no_controller` deploys the given bundle unchanged.
   - **Current-code attempt (second, or instead):** generate a new bundle from
     current code (`--current-state no_bundle`). This answers "does current
     code still fail". A bundle-generation failure means the original deploy
     error was never reached.
   - If local reproduction is not feasible (substrate unavailable, e.g.
     OpenStack, or sandbox capacity), record why, then continue at step 4
     (approval) and step 5 (fix, including its STOP before a PR). Skip step 6
     and verify through step 8 after a human merges the fix. Never merge.

   Record per attempt:
   - kind (replay / current-code / fix-verification) and what it checks;
   - inputs: execution link, bundle source, CIT commit, charm revisions,
     Juju and Kubernetes versions;
   - environment differences from the original run;
   - phase reached: bundle-gen, deploy, settle, or validate;
   - outcome, one of `same-error`, `not-reproduced`, `blocked-other-error`
     (the other error becomes a new signature), `inconclusive`, or
     `fix-confirmed` (only from a before/after pair under the same
     conditions);
   - what matches, differs, or is unknown, with evidence links.

4. **STOP for approval.** Present per-signature findings, confirmed cause,
   cause class, and proposed fix. Do not start fixing until the user approves.

5. **Fix at the correct layer**, routed by cause class:

   | Cause class | Route |
   | --- | --- |
   | Charm bug | `fix-charm-bug` in the upstream charm repo |
   | Topology, metadata, optional endpoints | `charm-override-investigator` |
   | Regressed neighbour charm | Pin via `charm-override-investigator`; no pin without an upstream issue and a review date. If nothing moves in about 3 months, the charm becomes a delisting candidate |
   | Bundle generation bug | Change `bundle_builder_x` following repo conventions. TODO: no dedicated skill yet |
   | Upstream (Juju, external) | `file-issue` |
   | Infrastructure | Escalate; record as tracked/accepted, not ours to fix |
   | Known and acceptable | `wontfix`, recorded as accepted |

   STOP before opening a PR or filing an upstream issue; use `file-pr` or
   `file-issue`, which have their own authorization rules. Do not default to
   longer timeouts, retries, or relaxed checks.

6. **Sandbox verification.** Repeat the reproducing attempt with the fix under
   the same conditions. Success means the run gets past the point of the
   original error. A later, different error is recorded as a new signature,
   not a failed fix. A pass or non-reproduction alone is not a fix.

7. **Report to Jira. STOP before posting** and show the drafts to the user.
   Post one top-level comment per signature; post every later update
   (attempts, fixes, PRs, rerun results) as a reply to that comment. Use the
   templates below.

8. **Pipeline verification (separate entry point,** e.g. "verify SQT-xxxx"),
   after the fix merged. Resume by reading the ticket's comment threads.
   Request reruns of the representative executions only with
   `create_rerun_requests` (`test_execution_ids`), skipping executions that
   already show `is_rerun_requested`. If the request is denied, report it to
   the user and stop. TODO: confirm that rerun requests are permitted. When the
   reruns finish, check
   whether each rerun still matches its signature; getting past the original
   error is success. STOP before posting: show the draft reply to the
   signature's comment and post it only after the user approves.

### Top-level comment template

```markdown
**Signature:** `test_deploy` | `failure:charm: <X>` | `failure:charm:<X>:status: <value>` | env: <optional>
**Representative execution(s):** <links>
**Baseline:** <N> results, query `<filters>`, run <YYYY-MM-DD>
**Initial label:** <from metadata>
**Confirmed cause:** <cause or "not yet confirmed"> (class: <cause class>)
**Status:** fixed (PR merged) | PR open | tracked (<link>) | accepted (<reason>)

- Confirmed facts: <observations with evidence links>
- Hypotheses: <unconfirmed explanations>
- Not verified: <item> - <reason> - <follow-up link or explicit acceptance>
```

### Reply template

```markdown
**Update:** attempt | fix | PR | rerun
**Kind / checks:** <attempt kind and what it checks>
**Inputs:** <execution, bundle source, CIT commit, charm revisions, Juju/K8s versions>
**Differences from original:** <list or "none known">
**Phase reached:** bundle-gen | deploy | settle | validate
**Outcome:** same-error | not-reproduced | blocked-other-error (<new signature>) | inconclusive | fix-confirmed
**Matches / differs / unknown:** <short list>
**Evidence:** <links>
```

## Definition of done

- Every confirmed issue is fixed or tracked.
- Every remaining unknown is linked to a follow-up issue or explicitly accepted.
- Reruns were done for fixed signatures.

Exceptions are allowed only when stated explicitly, with a reason, in the
signature's Jira comment.

## Guardrails

- Evidence over speculation; separate observations from hypotheses.
- A setup, bootstrap, or bundle-generation failure is not a reproduction.
- Clean up only run-owned resources; inspect state first.
- Do not present non-reproduction or a passing run as a fix.
- Do not commit, push, open PRs, file issues, or post to Jira without the
  stops above.

## Known gaps (v0)

- TODO: MCP access to saved bundles (the logs URL currently returns 403).
- TODO: live check of the `--current-state no_controller` bundle replay.
- TODO: rerun-request permission.
- TODO: dedicated skill for `bundle_builder_x` fixes.
- TODO: dedicated log-triage skill for `JujuWaitTimeoutError`.
- TODO: "typical filtering" for the epic pass rate beyond `wontfix`.

## Output

- Ticket, baseline query, date, and counted results.
- Per signature: filter, representative execution, initial label, confirmed
  cause and class, attempts with outcomes, fix and status.
- Drafted (or, if approved, posted) Jira comments.
- Cleanup status; state clearly what was not committed, pushed, or posted.
