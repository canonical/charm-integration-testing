# Comprehensive charm-quality observations

Snapshot: 20 October 2026. This supersedes the sampled report, preserved as
[REPORT-sampled.md](REPORT-sampled.md). Read-only investigation: no GitHub issues,
Test Observer attachments, rules or test results were changed.

## Main takeaway

> Charm quality means a supported integration contract, convergence despite
> different dependency/event ordering, and reliable recovery after deployment changes.

**The full population supports this message, but changes its emphasis.** Integration
and deployment requirements dominate the overall symptom inventory. Within reports
attributed to charm-side behavior, lifecycle recovery and reconciliation are more
prominent than integration-contract problems. Many other reported failures belong
to the harness or platform, or have no established owner.

## Scope: every linked GitHub report, not a sample

- Test Observer returned **454 charm-linked issue records**: **426 GitHub**, 27 Jira
  and one Launchpad. The 426 GitHub registrations were all fetched. A separate
  request at offset 454 returned no rows, and a subsequent refresh confirmed the
  same 426 GitHub IDs, URLs and reported execution counts.
- Following transfers/redirects and collapsing three alias pairs yields **423
  unique GitHub resources**. Four are pull requests, leaving **419 actual issues**
  across **54 canonical issue repositories**.
- **All 419 issue bodies and all 924 returned issue comments were reviewed**,
  including closure reasons and discussion that contradicts initial diagnoses.
  The four PR targets were reviewed and accounted for separately, not counted as
  issues. Nothing was selected by popularity, status or execution count.
- **112 issues are open; 307 are closed.** Of the closed issues, 273 have GitHub's
  `completed` reason, 18 `not_planned`, and 16 `duplicate`. A `completed` reason
  does not independently prove a shipped fix or a successful current rerun.
- Issue creation dates range from **September 2021 to October 2026**. This is a
  census of the currently linked historical issue population, not a recent
  failure-window analysis.

One GitHub issue reports one comment in metadata while the paginated endpoint
returns two distinct comments. Both returned comments were retained and reviewed;
this anomaly is recorded in the coverage ledger. There are no unresolved resource
lookups or missing review records. The earlier 14 unresolved lookups have all
been accounted for, including transferred issues and PR targets.

**Completeness boundary:** all GitHub references returned by Test Observer's charm
issue query, not every charm issue on GitHub. Unlinked issues, untriaged failures,
other trackers, and the contents of further linked tickets/source code/log archives
are not included in this census. This is a review of reports and their discussion,
not fresh reproduction of every alleged defect.

## What needs revision from the sampled report?

1. **Replace all top-50 prevalence figures.** The previous 40%, 10%, 83.4% and
   38.8% describe a selected high-association cohort, not this full population.
2. **Separate symptom from responsibility.** An unfulfilled endpoint does not
   establish a charm metadata bug, and a lifecycle timeout does not establish a
   charm recovery bug. Harness restrictions and Juju limitations recur throughout.
3. **Increase the emphasis on lifecycle resilience.** Lifecycle transitions are
   the largest observed theme among open reports, and the largest among reports
   attributed to charm-side behavior.
4. **Retract or qualify specific causal examples.** In particular, the MongoDB
   scale-to-zero example is attributed by later maintainer discussion to Juju
   storage reuse, not MongoDB primary-election logic. PgBouncer's exact migration
   race and ZooKeeper's departure-failure mechanism are not established.
5. **Use discussion, not just the opening body.** Some automated diagnoses and
   verified-fixed claims are explicitly rejected or retracted later. Closed
   status must not be treated as independent confirmation of those claims.

## Most commonly reported symptoms: all 419 issues

Every issue has one primary observed theme; additional concerns remain in its
individual annotation. These are frequencies of **issue reports**, not proportions
of charms, independent defects, or failed test executions. The percentages of open
reports use **112** as denominator, not the size of each theme.

| Primary observed theme | Issues | % of 419 | Open | % of 112 open |
| --- | --- | --- | --- | --- |
| Integration contract / metadata / topology | 113 | 27.0% | 22 | 19.6% |
| Lifecycle transitions and recovery | 71 | 16.9% | 30 | 26.8% |
| Configuration and deployment prerequisites | 68 | 16.2% | 18 | 16.1% |
| Test harness, scheduling and signal validity | 46 | 11.0% | 4 | 3.6% |
| Reconciliation, retries and readiness | 37 | 8.8% | 14 | 12.5% |
| Release / channel / Juju compatibility | 28 | 6.7% | 6 | 5.4% |
| Infrastructure / Juju / external platform | 16 | 3.8% | 7 | 6.2% |
| Other workload functionality / startup | 16 | 3.8% | 6 | 5.4% |
| Hardcoded environment assumptions | 14 | 3.3% | 3 | 2.7% |
| Observability, diagnostics and privacy | 5 | 1.2% | 0 | 0.0% |
| Resource ownership and deployment isolation | 4 | 0.9% | 2 | 1.8% |
| Insufficient evidence to classify | 1 | 0.2% | 0 | 0.0% |

Integration contract plus configuration/prerequisites account for **181/419
reports (43.2%)**. This includes unsupported generated test plans, solver/provider
limitations and test configuration, not just deficient charms.

Lifecycle recovery accounts for **30/112 open reports (26.8%)**, the largest open
category. Reconciliation/readiness adds **14/112 (12.5%)**. Combined, these are
**44/112 open reports (39.3%)**, across all responsible layers.

## Who is responsible? Do not equate a failure report with a charm defect

Responsibility is a conservative reading of the available report and discussion,
not a code audit. Repository location is not attribution: a charm repository may
contain a Juju bug, while a harness repository may document a real charm defect.
The charm category includes target charms, dependencies and shared charm code;
it does not blame the named charm-under-test automatically. Ambiguous or disputed
causal claims remain unknown rather than being forced into an owner bucket.

| Reported responsibility | Issues | % of 419 | Open |
| --- | --- | --- | --- |
| Test harness / solver / fixtures / validators | 153 | 36.5% | 17 |
| Responsibility not established | 120 | 28.6% | 47 |
| Charm-side (target, dependency or shared charm code) | 86 | 20.5% | 32 |
| Infrastructure / Juju / external platform | 33 | 7.9% | 11 |
| Deployment configuration / unsupported topology | 23 | 5.5% | 4 |
| Upstream workload | 4 | 1.0% | 1 |

**153/419 (36.5%)** are attributed to the harness, **86/419 (20.5%)** to charm-side
behavior, and **120/419 (28.6%)** have responsibility not established. The last
category must not be counted as either healthy charms or confirmed charm bugs.
These are interpreted report attributions, not statistically validated root causes.

### Developer-focused view: the 86 charm-attributed reports

This is a filtered view of the complete census, not a sample. It is useful for
prioritizing advice, but is not an exhaustive count of actual charm defects:
unresolved reports may also contain them, and several reports can describe the
same underlying defect.

| Primary theme among charm-attributed reports | Issues | % of 86 | Open |
| --- | --- | --- | --- |
| Lifecycle transitions and recovery | 26 | 30.2% | 8 |
| Reconciliation, retries and readiness | 18 | 20.9% | 8 |
| Integration contract / metadata / topology | 11 | 12.8% | 5 |
| Hardcoded environment assumptions | 9 | 10.5% | 3 |
| Configuration and deployment prerequisites | 7 | 8.1% | 4 |
| Release / channel / Juju compatibility | 7 | 8.1% | 2 |
| Other workload functionality / startup | 5 | 5.8% | 2 |
| Observability, diagnostics and privacy | 2 | 2.3% | 0 |
| Resource ownership and deployment isolation | 1 | 1.2% | 0 |

**Lifecycle recovery plus reconciliation/readiness account for 44/86 (51.2%)** of
the charm-attributed reports, and **16/32 (50.0%)** of the open charm-attributed
reports. This is the clearest reason to lead developer recommendations with
recovery and convergence rather than simply installation or metadata hygiene.

## Recommendations and supporting examples

### 1. Treat lifecycle recovery as part of the feature

Test pod/controller restart, migration where supported, relation removal and
restoration, upgrades, and scale-in/out including zero where supported. Check
application-scoped resource survival, storage reattachment, configuration
regeneration and eventual workload health. Document intentional unsupported
transitions and required operator actions.

- [canonical/katib-operators#379](https://github.com/canonical/katib-operators/issues/379),
  **open**: scaling to zero deletes a ServiceAccount subsequently needed to create
  pods. This is a concrete application-resource lifetime problem.
- [canonical/mongodb-k8s-operator#590](https://github.com/canonical/mongodb-k8s-operator/issues/590),
  **open**: the observed primary-election wait remains real, but later maintainer
  discussion traces the failure to **Juju not reusing storage on scale-up**.
  Retain it as a lifecycle-test example, not an established charm algorithm bug.
- [juju/juju#23281](https://github.com/juju/juju/issues/23281), **closed**:
  discussion states that migration between 4.0 controllers is unsupported.
  Identical controller versions alone do not establish a supported migration path.

### 2. Reconcile current state; make repeated events safe

Avoid depending on a unique hook order or a deferred event surviving pod
replacement. Re-evaluate integrations when prerequisites change. Make retries
idempotent, and detect/repair drift. Test missing data followed by later completion,
reversed event order, partial progress, repeated hooks and replacement pods.

- [canonical/hydra-operator#560](https://github.com/canonical/hydra-operator/issues/560),
  **closed**: discussion replaces the initial uncommitted-event explanation with
  pod replacement wiping ephemeral deferred-event state. Holistic reconciliation
  is discussed; closure alone does not demonstrate a delivered fix.
- [canonical/opentelemetry-collector-k8s-operator#237](https://github.com/canonical/opentelemetry-collector-k8s-operator/issues/237),
  **open**: repeated dashboard processing produces oversized filenames and failed
  hooks. Later discussion says more than storage-attached is affected; ownership
  between the charm and dashboard library remains unresolved.
- [canonical/istio-operators#551](https://github.com/canonical/istio-operators/issues/551),
  **open**: the charm does not correct webhook configuration drift. Later reports
  add shared-cluster interference; unsupported probability claims in that
  discussion should not be repeated as measured prevalence.

### 3. Publish a complete supported integration contract

Provide a minimal deployable bundle per track. Declare relation cardinality,
required companion relations, permissions and support assumptions. Document
external services, credentials, TLS and role-specific configuration. Negative
configurations should yield actionable status instead of unrelated tracebacks.
Validate metadata and code assumptions together.

- [canonical/wordpress-k8s-operator#336](https://github.com/canonical/wordpress-k8s-operator/issues/336),
  **open**: metadata permits two database relations, while the implementation
  raises `TooManyRelatedAppsError` when retrieving a unique relation.
- [canonical/charm-integration-testing#265](https://github.com/canonical/charm-integration-testing/issues/265),
  **closed**: a maintainer rejects the initial harness-only diagnosis and says
  ingress was incorrectly marked optional in charm metadata.
- [canonical/charm-integration-testing#887](https://github.com/canonical/charm-integration-testing/issues/887),
  **open**: explicitly documents unsupported topologies despite matching interface
  names. These plans should not automatically count as broken charm integrations.

Do not fix every unsatisfied endpoint by making it optional, granting broad trust,
or forcing deployment. Resolve the contract, topology or permission requirement.

### 4. Validate support boundaries across the whole dependency chain

Test supported Juju/base/channel combinations with real dependencies and
track-aware bundle configuration. Publish compatibility constraints and plan
for obsolete revisions. Distinguish product certification from technical ability
to force an experimental deployment.

- [canonical/postgresql-k8s-operator#1464](https://github.com/canonical/postgresql-k8s-operator/issues/1464),
  **closed**: a maintainer explains that `assumes` intentionally blocks Juju 4 by
  default. This is not proof of a broken runtime implementation; forced laboratory
  deployment is not a support warranty.
- [canonical/charm-integration-testing#489](https://github.com/canonical/charm-integration-testing/issues/489),
  **closed**: a MinIO dependency uses the removed `pod-spec-set` API. The healthy
  target should not inherit blame for incompatible bundle selection.
- [canonical/charm-integration-testing#235](https://github.com/canonical/charm-integration-testing/issues/235),
  **open**: a broad umbrella for obsolete published revisions. Its **15,471 reported
  associations** are not 15,471 independent defects or a single confirmed cause.

### 5. Remove environment assumptions and define resource ownership

Exercise arbitrary model/application/namespace names and concurrent deployments
on a shared cluster. Obtain addresses and credentials from configuration or
relation contracts, rather than hardcoded names. Reconcile owned cluster-scoped
resources and clean them up without removing another application's resources.

- [canonical/discourse-k8s-operator#413](https://github.com/canonical/discourse-k8s-operator/issues/413),
  **open**: an application-name fallback produces an invalid OAuth callback URL.
  The claimed connection to a later database failure is not established.
- [canonical/kfp-operators#389](https://github.com/canonical/kfp-operators/issues/389)
  and [kubeflow/kubeflow#6136](https://github.com/kubeflow/kubeflow/issues/6136),
  both **closed**, document historical hardcoded service/namespace assumptions.
  They are preventative lessons, not evidence current releases remain broken.
- [canonical/istio-operators#551](https://github.com/canonical/istio-operators/issues/551)
  illustrates why namespace deletion alone cannot be assumed to remove
  cluster-scoped webhook resources.

### 6. Treat diagnosis and fix verification as quality work

For both charm teams and QA: identify the first failing stage and actual component,
retain source/destination controller evidence before cleanup, and distinguish
functional correctness from readiness and elapsed-time checks. Do not infer cause
from the last timeout or repeat an AI hypothesis as a verified finding.

- [canonical/charm-integration-testing#473](https://github.com/canonical/charm-integration-testing/issues/473):
  a maintainer retracts an earlier verified-fixed closure; the actual MySQL Router
  plan still produced the wrong dual-relation bundle.
- [canonical/charm-integration-testing#987](https://github.com/canonical/charm-integration-testing/issues/987):
  validating only the first channel missed wider override breakage; the report
  includes an all-channel reproduction with 13 failures and 39 passes.
- [canonical/charm-integration-testing#874](https://github.com/canonical/charm-integration-testing/issues/874):
  historical failures prompted a report after matching errors had already been
  fixed. Use run timestamps relative to fixes and require current recurrence.

## Specific corrections to the previous report's examples

| Previous use | Revision required by full discussion |
|---|---|
| MongoDB scale-to-zero as a charm recovery defect | Retain observed lifecycle failure; attribute the reported mechanism to Juju storage reuse, per maintainer discussion. |
| PgBouncer exporter failure as a proven post-migration config race | The backoff symptom is supported, but similar failures occur without migration and the original explanation cites a PostgreSQL path. Root cause remains unknown. |
| ZooKeeper logging-provider removal as an established departure-handling bug | Retain the removal-associated failure, but a later proxy-library explanation is unverified. Do not claim a proven cleanup mechanism. |
| PostgreSQL/Juju 4 bundle resolution as an unqualified compatibility gap | Explain the intentional support gate and provider filtering; do not recommend force as a production fix. |
| Hydra deferral example | Use pod replacement/ephemeral-state loss from later discussion, not the initial uncommitted-event theory. |
| Closed reports as historical fixed defects | Call them closed reports; a closure can be duplicate, not planned, stale, or based on a retracted verification. |

## Measurement: issue prevalence, not failure prevalence

The main denominator is **419 unique issues**, with **112 open issues** as a
separate backlog view. These frequencies are descriptive interpretations of the
linked report corpus. Distinct issue numbers are not necessarily independent
bugs; duplicate reports remain distinct issues unless they are the exact same
canonical GitHub resource reached through aliases. Historical catch-alls and
changes in reporting practice affect the distribution.

The registry's summed `test_executions_count` is **144,587** across all 426 GitHub
registrations. Four PR targets account for **889**, leaving **143,698** on issue
registrations. These are **association weights**, not deduplicated failed
executions. An execution may link to multiple issues; aliases may duplicate
associations; rules can accumulate a broad signature. The API does not establish
that each issue's aggregate count is restricted to charm-family executions.
Accordingly, these weights are retained for audit, not used to rank independent
causes or calculate failure rates.

No recent all-execution denominator was obtained: broad Test Observer execution/
result searches timed out during the earlier investigation. The earlier three
historical source-result checks remain useful illustrations, not census-wide
reproductions. No claim is made about the percentage of charms affected, current
pass rates, defect introduction rates, or improvements over time.

For future incidence metrics, obtain a fixed recent window of all eligible
executions, deduplicate execution IDs, and explicitly decide how to handle reruns,
archived revisions and superseded runs. Separate target/dependency/harness/platform
failures, primary causes and collateral errors, and stratify by supported lifecycle
stage and environment. Report affected executions / eligible executions and
number of affected charm names / tested charm names separately.

## Audit trail and reproducibility

- [Complete 419-issue index and four PR exceptions](census/ALL_ISSUES.md)
- [All-resource evidence ledger](census/all-issues-evidence.csv): 423 rows,
  covering all 426 registrations; includes status, theme, responsibility,
  verbatim evidence, source URL, discussion corrections and caveats.
- [Census metrics](census/metrics.json)
- [Coverage, aliases and redirects](census/coverage.json)
- [Full canonical bodies and discussions](census/canonical-corpus.json)
- [Captured Test Observer population](census/test-observer-snapshot.json)
- [Review annotations](census/reviewed-corpus.json)
- [Aggregation and validation script](census/aggregate.py)
- [Report renderer](census/render_report.py)

The aggregation asserts exact input/output ID equality in every review partition,
unique IDs, valid categories, 419 issues / four PRs / 426 registrations, reconciled
association totals, and source-valid evidence URLs and verbatim quotations. All
checks pass with **zero source-validation warnings**. These checks establish
coverage and traceability, not infallibility of the interpretive classifications.

From this directory, run `python3 census/aggregate.py` and
`python3 census/render_report.py` to regenerate the metrics, ledger and report.
