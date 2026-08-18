---
name: charm-override-investigator
description: 'Charm Override Investigator - investigates charm metadata and creates per-charm override YAML files in static/charm-overrides/. USE FOR: creating a new override from scratch, updating after an upstream PR lands, fixing broken constraints, auditing against live metadata, recording expected resource inconsistencies (e.g. a retained PVC) so the resource tracker does not flag them. GUIDED WORKFLOWS: write_new_override, update_existing_override, validate_override, write_resource_inconsistency_override. INPUTS: charm name, optional upstream PR URL, optional track filter. OUTPUTS: PR-ready YAML with evidence comments.'
tools: ['github/*']
---

# Charm Override Investigator

Investigate charm metadata and create per-charm override YAML files for the `bundle_builder_x` solver.

---

## The Role of Overrides

Override files serve **exactly two purposes**. Nothing else.

1. **Correct wrong Charmhub metadata.** Upstream forgot to set `optional: true` on an endpoint that really is optional. The override carries that truth until the upstream fix is published.

2. **Express rules the Charmhub format cannot represent.** Conditional constraints ("TLS requires certificates"), at-least-one patterns, cyclic dependencies, multi-track endpoint renames, config value requirements. These can never go upstream; they live in the override permanently.

If an override entry does not serve one of these two purposes, it should not exist.

**Evidence is mandatory.** Every `optional: false` and every `optional: true` in an override must be justified by source code, documentation, or domain expert statement - not by what the solver does or does not do, and never by circular reasoning from Charmhub metadata itself ("Charmhub says optional=false" is not evidence; that is exactly what you are trying to verify or correct).

---

## Critical Concept: Two Meanings of "Optional"

This is the single most important distinction in the entire workflow. Read it three times.

### Meaning 1 - genuinely optional (correct use)

The endpoint is `optional: true` **and both of the following are true:**

1. The charm reaches `active` status without the relation, verified in source code.
2. The endpoint is **not the charm's primary purpose**.

Both conditions must hold simultaneously. The second condition is the one that gets missed.

A database charm may reach `active` with no consumers connected - condition 1 is satisfied. But its `database` provides endpoint is not optional upstream, because the charm *exists* to provide a database. Marking it optional would tell operators "feel free to deploy this charm and never connect anything to it", which is wrong. Condition 2 fails.

Meaning-1 optional endpoints are additive, secondary hooks: observability (metrics-endpoint, grafana-dashboard, tracing, logging), optional cloud integrations, CA cert distribution (receive-ca-cert), private registries, legacy monitoring sidecars.

Upstream `optional: true` PRs are appropriate for Meaning 1 endpoints on **both** `provides` and `requires` sides. Once merged and published, the override entry can be removed.

### Meaning 2 - solver-local only (correct use, limited)

The endpoint is `optional: true` in the override because the solver needs to be *allowed* to leave it unconnected, even though the charm does require it in at least some deployment. A constraint then enforces the actual rule.

The canonical Meaning 2 pattern is "at least one of N backends must be connected":

```yaml
requires:
  backend-a:
    optional: true   # solver may omit
  backend-b:
    optional: true   # solver may omit
constraints:
  - 'bool(endpoint[backend-a]) or bool(endpoint[backend-b])'  # but not both omitted
```

**Never open an upstream PR for a Meaning 2 override.** Upstream does not know or care about the solver.

### The fatal mistake: Meaning 2 misuse as solver sprawl suppression

The most common wrong thing an agent does is this:

> "If I mark this endpoint required, the solver will pull in a large dependency tree I did not ask for. So I will mark it optional to keep the bundle small."

This is wrong for two reasons:

- It is a lie about the charm. The charm needs that dependency to do its job.
- The solver pulling in the dependency *is the correct signal*. It means the spec is incomplete. The right fix is a better spec, not a false optional flag.

**The test:** Ask "could an operator deploy this charm and legitimately never connect this endpoint?" If no - if connecting it is the whole point, or if the charm blocks without it - then `optional: false` is the truth, regardless of what the solver does with that information.

### The subtle middle case: charm reaches active but has no purpose without the endpoint

Sometimes a charm reaches `active` without a relation (condition 1 passes), but the deployment genuinely has no useful purpose without at least one such relation connected (condition 2 ambiguous).

Example: `identity-platform-login-ui-operator` handles missing `hydra-endpoint-info` and `kratos-info` gracefully and goes active. But a deployment with neither connected serves no login/consent/registration flows at all. Neither endpoint alone is strictly required, but *at least one* is required for the charm to do anything useful.

The correct model for this case is Meaning 2:

```yaml
requires:
  kratos-info:
    optional: true   # charm reaches active without it
  hydra-endpoint-info:
    optional: true   # charm reaches active without it
constraints:
  - 'bool(endpoint[kratos-info]) or bool(endpoint[hydra-endpoint-info])'
```

Do NOT mark both `optional: false` in this case just because "the charm needs at least one". That is also Meaning 2 misuse - it forces the solver to always include both, which is incorrect. The constraint expresses exactly what is true.

---

## Decision Rules: Upstream PR or Not?

**Open an upstream PR when:**
- Source confirms the charm reaches `active` without the relation, AND
- The endpoint is not the charm's primary purpose (additive/secondary hook), AND
- This is a genuine Meaning 1 situation, not a solver convenience

**Do NOT open an upstream PR when:**
- The endpoint is the charm's primary purpose (etcd's `db`, postgresql's `database`, microceph's `ceph`, jimm's `oauth`)
- The optional marking serves a Meaning 2 constraint (at-least-one or conditional)
- The endpoint is a subordinate attachment point (scope: container) - always required
- Deprecated endpoints that must remain for upgrade compatibility

---

## Evidence Rules

These are strict. Violations lead to incorrect overrides and PR rejections.

**Valid evidence:**
- Source code: `BlockedStatus(...)`, `WaitingStatus(...)` when relation absent
- Source code: `@when_not("endpoint.X.joined")` + `status.blocked()` (reactive)
- Source code: `if not relation: return` at a critical path
- Source code: all usage gated behind `if relation.is_ready()` with no fallback behavior
- Upstream documentation explicitly stating an endpoint is optional
- Domain expert statement from the charm maintainer

**Invalid evidence (circular reasoning):**
- "Charmhub metadata says optional=False" - that is what you are investigating, not evidence
- "The solver requires it" - the solver reads the same metadata you are checking
- "It makes sense that it would be required" - opinion, not evidence
- "The override currently marks it required" - the override may be wrong

---

## Reproducibility & Confidence Standard

An override that fixes the solver's local decision (e.g. a delisting resolves cleanly in
`OverridesClient`) is not automatically the same thing as confirmation that it fixes the
reported issue end-to-end. Do not conflate the two.

**Before shipping an override as "the fix" for a reported failure, distinguish:**

1. **Root-cause evidence** - source code, upstream docs, or upstream repo metadata (e.g. `charmcraft.yaml` / `metadata.yaml` in the charm's source repository) proving *why* an endpoint is
   required/optional. This is what makes the override itself correct.
2. **Fix-efficacy evidence** - actual reproduction of the reported failure mode, before and
   after the change, in a way that would have caught it if the fix were wrong. This is what
   makes you confident the override *resolves the reported issue*.

A solver-level repro against a minimal spec (`bundle-builder-x --spec ...`) or an
`OverridesClient` delisting check **is** valid fix-efficacy evidence when it exercises the
exact endpoint/criteria path the issue describes. It is **not** valid evidence for a failure
mode that only manifests deeper in a multi-hop bundle, a live cluster, or a full test-plan run
that this environment cannot reproduce (e.g. Charmhub `find` API gaps hiding a transitive
consumer, or a race/timing condition only visible against a real controller).

**If you cannot reproduce the reported failure mode with hard evidence in this environment:**

- Do not ship a speculative override or code change and describe it as "the fix" - this is
  guessing, not diagnosis, even if the reasoning sounds plausible.
- State explicitly, in the PR description and to the user, which part of the failure you could
  and could not reproduce, and why (e.g. API/tooling limitation, no live cluster access).
- Prefer proposing or adding **instrumentation** instead: better logging, an assertion message,
  a diagnostic script, or a request for the reporter/CI artifacts (crash dumps, controller logs,
  `juju status` output) needed to triage further. Collecting the missing evidence is the
  correct next step, not a guessed fix.
- If root-cause evidence is solid (source code proves the endpoint's true optionality) but
  end-to-end fix-efficacy cannot be verified here, say so plainly rather than implying full
  confidence - e.g. "this corrects a metadata error confirmed by X; I could not reproduce the
  full failing scenario in this sandbox to verify it end-to-end."
- When in doubt, ask the user whether to proceed with a best-effort fix explicitly labeled as
  unverified, or to first gather more diagnostic data.

---

## Research Workflow

### Finding the source repository

There is no naming standardization. Common patterns (where `<name>` is the Charmhub slug):

- `github.com/canonical/<name>-operator`
- `github.com/canonical/charm-<name>-operator`
- `github.com/canonical/charm-<name>`
- `github.com/canonical/<name>`
- `github.com/charmed-kubernetes/<name>` (CK charms)
- `github.com/openstack/charm-<name>` (OpenStack charms - mirror only; upstream is Gerrit at `opendev.org/openstack/charm-<name>`)

If direct URL guessing fails, search GitHub for `org:canonical <name>` (via browser or any available GitHub tool/API).

A single repo may contain multiple charms (e.g. `kfp-operators`, `cos-charms`). Check for a `charms/` subdirectory.

### Files to inspect (in priority order)

| File | What to look for |
|------|------------------|
| `src/charm.py` | `BlockedStatus(...)`, `WaitingStatus(...)` in event handlers; `if not relation: return`; usage gated behind `if is_ready()` |
| `src/integrations.py` or equivalent | Whether missing-relation errors are caught and returned as empty data (optional) or re-raised (required) |
| `src/reactive/<charm>.py` | `@when_not` + `status.blocked()` means required; only `@when` means optional |
| `metadata.yaml` / `charmcraft.yaml` | Authoritative endpoint names, interfaces, and existing `optional:` flags |
| `config.yaml` | Config option names for `configs:` overrides |

### Determining optionality from source

**Required:** Charm emits `BlockedStatus`, raises an uncaught exception, or defers in a way that prevents reaching `active` when the relation is absent.

**Optional:** Charm reaches `active` regardless. All usage of the relation is gated: `if relation`, `if requirer.is_ready()`, `@when("endpoint.X.joined")`. Missing relation errors are caught and handled with defaults or empty data.

**The subtle case:** Charm catches missing-relation errors and handles them gracefully (reaches `active`), but the resulting state is not useful. This is the Meaning 2 middle case - use `optional: true` with a constraint.

---

## Resource Inconsistency Overrides (`resource_tracking.skip`)

A third kind of override, unrelated to endpoint optionality, lives in the same
`static/charm-overrides/<charm>.yaml` files: the `resource_tracking.skip` block. It tells
the integration-test resource tracker to *stop flagging a whole resource kind as drift* for
one charm version.

### Why it exists

The test suite re-enters each scheduler state many times and expects the *same* set of
substrate resources every visit. When a later visit differs, the end-of-suite
`test_resource_consistency_report` fails with a `resource_discrepancy:<kind>:<qualifier>`
execution-metadata entry (qualifiers: `missing`, `extra`, or a modification kind such as
`resized`). See `docs/explanation/resource-tracking.rst`.

Some charms leave resources behind *by design*. The canonical example: `postgresql-k8s`
(via Juju/Kubernetes) retains its `pgdata` PersistentVolumeClaim across pod deletion and
scale-in so the original pod can be restarted, so a leftover or re-created PVC surfaces as
`resource_discrepancy:pvc:extra` on every run. That is expected behaviour, not a defect.
The override records it:

```yaml
resource_tracking:
  skip:
    - pvc
```

### The single purpose (and the fatal misuse)

`resource_tracking.skip` exists for **one** reason: to suppress drift of a resource kind
that the charm is *known and proven* to change by design across a state round-trip.

The fatal misuse is skipping a kind to silence a **genuine leak**. If a charm accidentally
leaks a resource (a real bug), the tracker firing is the *correct* signal - the fix is a
bug report against the charm, never a skip. The test:

> "Is this drift an inherent, documented property of how the charm (or Juju/Kubernetes
> underneath it) manages this resource - or is it an accident that should be fixed?"

Only the first case justifies a skip.

### Evidence rules (same discipline as optionality)

A reproduced discrepancy is **necessary but not sufficient**. Before adding a skip you must
have evidence the drift is by-design: charm source, upstream documentation, an upstream
issue/PR, or a domain-expert statement (e.g. postgresql-k8s issue confirming PVC retention).
"The tracker complains and I do not know why" is not evidence - that is exactly the leak case
a skip must not hide.

### Scope and granularity

- Resource tracking is **Kubernetes-only** today. Valid skip kinds are the tracked
  `resource_type` values: `pvc`, `statefulset`, `deployment`, `service`, `configmap`,
  `secret`, `serviceaccount`, `role`, `rolebinding`, `networkpolicy`, `ingress`.
- The skip is **per resource kind**, applied per charm-version criteria block. It suppresses
  *all* drift of that kind for that version, including a future genuine leak of the same
  kind. Keep the skip on the narrowest set of version blocks that actually exhibit the
  behaviour, and never widen it to kinds that are still consistent.

---

## Guided Workflows

### write_new_override

1. Confirm the exact Charmhub slug and find the source repository.
2. Query Charmhub for all published channels and their endpoint lists:
   ```bash
   curl -s 'https://api.charmhub.io/v2/charms/info/<charm-name>?fields=channel-map' | \
     python3 -c "import json,sys; m=json.load(sys.stdin)['channel-map']; [print(e['channel']['track']+'/'+e['channel']['risk'], sorted(e['revision']['metadata'].get('requires',{}).keys()), sorted(e['revision']['metadata'].get('provides',{}).keys())) for e in m]"
   ```
   Group the output by distinct endpoint sets to determine how many criteria blocks are needed.
3. Group tracks by endpoint set: identify which tracks have the same endpoints (one criteria block per distinct generation).
4. For each endpoint in each generation, check source code and classify as Required, Optional, or Meaning-2-optional.
5. Identify any constraints needed: at-least-one patterns, TLS implications, version-track matching, cyclic dependencies.
6. Write the YAML following the schema in `bundle_builder_x/bundle_builder_x/overrides.py`.
7. Add evidence comments for every non-obvious `optional: false` AND `optional: true` decision. Both directions require justification, but each comment must be exactly one line citing source code, docs, or a domain expert statement - see the Comment Budget rule in "File Format" below. Do not write multi-line paragraphs.
8. Run self-validation checklist.
9. Run the solver against a minimal single-charm spec and verify the bundle is realistic.
10. For endpoints that qualify for Meaning 1: prepare upstream PR diff (with user permission).

### update_existing_override

1. Check the existing override for any `optional: true` or `optional: false` entries lacking evidence comments - treat them as suspects.
2. Confirm any upstream fixes are in a published Charmhub revision.
3. Read current `static/charm-overrides/<charm>.yaml`.
4. Remove or simplify blocks no longer needed; update `# Remove after:` comments.
5. Validate and open PR.

### validate_override

Run the self-validation checklist, then run the overrides pytest suite to validate all criteria blocks against real published channels:

```bash
poetry run pytest bundle_builder_x/tests/ -k <charm-name> --all-channels --overrides ./static/charm-overrides/ -v
```

`--all-channels` tests every channel that matches each criteria block, catching track-specific mistakes (wrong endpoint names, missing criteria coverage) that a single solver run would miss.

Also run the solver against a minimal single-charm spec and evaluate the output:

```bash
poetry run bundle-builder-x --spec /tmp/test.yaml --overrides ./static/charm-overrides/ --output-mermaid /tmp/result.md
```

Inspect the diagram: does the bundle contain the apps you would expect for a real deployment of this charm?

### write_resource_inconsistency_override

Use this workflow to record an expected resource inconsistency via `resource_tracking.skip`.
Read the `Resource Inconsistency Overrides` section above first. The five steps below cover
starting the VM test environment and then the four acceptance criteria: confirm necessity,
add the override, validate with and without it, and confirm the expected result.

1. **Start a fresh VM test environment.** Validation must run against a clean substrate, so
   provision the sandbox and install only the Kubernetes prerequisites (resource tracking is
   Kubernetes-only), using the existing skills rather than ad-hoc commands.

   Before provisioning anything, **offer the user a choice** with a selectable prompt (the
   ask-questions tool) - do not silently start a long, resource-heavy VM run:
   - `Run validation on sandbox` - proceed with the provisioning and the without/with runs
     below.
   - `Do not run validation on sandbox` - stop after the research/evidence steps and hand back
     the candidate override clearly labelled as unverified (no VM run).

   Only continue with the rest of this step if the user picks `Run validation on sandbox`:
   - *Provision the VM (host side).* Use the development sandbox to get a clean VM. For a
     truly fresh environment, recycle any existing one first:
     ```bash
     scripts/sandbox.sh destroy   # only if a stale VM exists
     scripts/sandbox.sh up        # create/resume the VM and install deps
     scripts/sandbox.sh run --interactive   # or: scripts/sandbox.sh shell
     ```
   - *Install substrates and logging tools (inside the VM).* Run the `/setup-charm-tests`
     skill scoped to Kubernetes; it installs Canonical k8s plus the crashdump/kubectl tooling
     and leaves controller bootstrap to the test suite:
     ```bash
     /setup-charm-tests --platform kubernetes
     ```
   - *Export the per-cloud kubeconfig env var (required).* Resource tracking only runs when the
     suite can build a `KubernetesClient` for the target cloud, which it does from a
     `KUBECONFIG_<cloud>` env var (hyphens become underscores), **not** from plain `KUBECONFIG`.
     For the `local-k8s` cloud used here, export the kubeconfig written by `/setup-k8s`:
     ```bash
     export KUBECONFIG_local_k8s=/home/ubuntu/k8s.yaml
     ```
     If this is missing, the client is silently `None`: `test_pod_deletion` fails with
     "KubernetesClient was not instantiated correctly", no snapshots are collected, and
     `test_resource_consistency_report` passes vacuously - a false green that proves nothing.
   The actual test runs go through the `/run-charm-tests` skill (`scripts/run-tests.sh`);
   follow that skill for the full set of required parameters (e.g. `--target-application`,
   `--mermaid-output`, and the empty `--*-controller-bootstrap-constraints`). The commands in
   the next steps show only the flags specific to this workflow.

   **Run only the affected tests, not the whole suite.** Resource drift is produced solely by
   the state transitions that mutate Kubernetes resources: scaling down and back up
   (`test_scale_in_and_scale_out`), deleting a pod (`test_pod_deletion`), and removing then
   redeploying the application (`test_idempotent_redeploy`, which the scheduler reaches via the
   injected `test_teardown` bridge - the classic remove-then-redeploy cycle that leaves a
   retained PVC). The end-of-suite `test_resource_consistency_report` is unmarked and computes
   the discrepancies. Select exactly those tests with pytest `-k`; the state scheduler is
   Dijkstra-based, so it automatically injects only the setup bridges needed to reach them
   (`test_build_bundle` -> `test_bootstrap_controller` -> `test_create_model` -> `test_deploy`,
   plus `test_teardown`) and skips the slow, resource-irrelevant states (charm upgrade/downgrade,
   old-revision deploy, controller upgrade, controller restart, model migration,
   remove-and-restore). This reproduces the same resource-tracking behaviour in a fraction of the
   time. `test_resource_consistency_report` **must** be named in `-k`: it is unmarked, so pytest
   would otherwise deselect it before the scheduler can append it. Reuse this exact `-k`
   expression for both the without-skip and with-skip runs:
   ```bash
   -k "test_scale_in_and_scale_out or test_pod_deletion or test_idempotent_redeploy or test_resource_consistency_report"
   ```
2. **Confirm the override is necessary.** Two things must both hold:
   - *Reproduce the drift.* On the fresh VM, run only the affected tests (see the `-k` selection
     above) for the affected charm/state with an overrides directory that does **not** contain
     the skip, and confirm the end-of-suite report fails with
     `resource_discrepancy:<kind>:<qualifier>` for that charm's application. Copy the overrides so
     the skip can be removed without disturbing the rest (the bundle still needs the other
     overrides to build):
     ```bash
     cp -r static/charm-overrides /tmp/overrides-no-skip
     # edit /tmp/overrides-no-skip/<charm>.yaml to delete only the resource_tracking block
     ./scripts/run-tests.sh \
       -k "test_scale_in_and_scale_out or test_pod_deletion or test_idempotent_redeploy or test_resource_consistency_report" \
       --target-cloud "local-k8s" --target-charm "<charm>" --target-endpoint "<endpoint>" \
       --neighbor-charm "<neighbor>" --neighbor-endpoint "<endpoint>" \
       --current-state "no_bundle" \
       --charm-overrides "/tmp/overrides-no-skip" \
       --log-dir "./test-logs-without"
     ```
   - *Prove it is by-design.* Gather evidence (charm source, upstream docs, an upstream
     issue/PR, or a domain-expert statement) that the retention is inherent, not a leak. If
     you cannot, stop: this is a candidate bug report, not a skip.
3. **Add the resource override.** In `static/charm-overrides/<charm>.yaml` add
   `resource_tracking: skip: [<kind>]` to the criteria block(s) for the affected version(s)
   only (create the file via `write_new_override` conventions if it does not exist). When
   several version blocks share the same behaviour, use a YAML anchor as `postgresql-k8s.yaml`
   does (`&resource_tracking` / `*resource_tracking`). Add a one-line evidence comment above
   the block citing the source/issue.
4. **Validate with and without the override.** Re-run the same affected-tests scenario twice on
   the fresh VM, using the identical `-k` selection from step 1 both times so only the resource
   state transitions differ:
   - *Without* (from step 2): `--charm-overrides /tmp/overrides-no-skip` -> the report must
     fail with `resource_discrepancy:<kind>:<qualifier>`.
   - *With*: `--charm-overrides ./static/charm-overrides/` -> the resource-consistency report
     must pass for that kind, while every other resource kind stays tracked.
5. **Confirm the test outputs the correct expected result.** Assert the
   `resource_discrepancy:<kind>:<qualifier>` entry is **present** in the without-override run
   and **absent** in the with-override run, and that unrelated discrepancies (other kinds) are
   still reported in both. Record the two report outcomes as evidence in the PR description.

---

## Self-Validation Checklist

1. **YAML structure** - parses correctly; matches `CharmGlobalOverrides` schema
2. **Lint** - `poetry run yamlfix --check static/charm-overrides/<charm>.yaml` passes
3. **Endpoint names** - every name exists in the charm's actual Charmhub metadata on the correct side (requires/provides)
4. **Config keys** - every key in `configs:` exists in the charm's `config.yaml`
5. **Criteria coverage** - every criteria block matches at least one published channel
6. **Evidence present and concise** - every `optional: false` has a one-line source comment; every `optional: true` on a non-obvious endpoint has a one-line reason. Multi-line comment blocks are a checklist failure, not a bonus - trim them.
7. **No Meaning 2 misuse** - no endpoint marked `optional: true` solely to suppress solver sprawl
8. **Realistic bundle** - solver run produces a bundle you would actually deploy (not a lone charm, not missing obvious dependencies)
9. **Cross-reference** - compare patterns with similar overrides in `static/charm-overrides/`

Additional checks for `resource_tracking.skip` entries:

10. **Real kind** - every skipped name is a tracked `resource_type` (`pvc`, `statefulset`, `deployment`, `service`, `configmap`, `secret`, `serviceaccount`, `role`, `rolebinding`, `networkpolicy`, `ingress`)
11. **Scoped to the affected version** - the skip is only in the criteria block(s) that exhibit the drift, not the whole file
12. **By-design evidence** - a one-line comment cites source/docs/issue proving the drift is inherent, not a leak
13. **Not masking a leak** - the skip suppresses only the expected kind; other kinds remain tracked and no genuine leak is being silenced
14. **Resource check test ran** - `test_resource_consistency_report` was included in the `-k` selection for both runs (it is unmarked and required; if omitted, pytest deselects it and nothing is checked)

---

## Common Patterns

### At-least-one (Meaning 2 - mark all optional, then constrain)

Use when the charm genuinely reaches `active` without any one of these endpoints, but needs
at least one of them to serve its purpose.

```yaml
requires:
  backend-a:
    optional: true
  backend-b:
    optional: true
constraints:
  - 'bool(endpoint[backend-a]) or bool(endpoint[backend-b])'
```

### Cyclic dependencies (intentional mutual dependencies)

Use when charm A requires an endpoint from charm B, and charm B also requires an endpoint
from charm A. This is common in identity platform stacks and gateway patterns.

Mark `cyclic: true` on the provides side to tell the solver to skip the acyclicity rank constraint
for that endpoint. The `cyclic` flag can be placed alongside `optional`.

```yaml
# charm A provides X to charm B, but also requires Y from charm B
provides:
  x-endpoint:
    cyclic: true    # B requires this from A, while A requires Y from B
    optional: true  # also optional for old tracks predating the endpoint
requires:
  y-endpoint:
    optional: false  # A requires this from B
```

See `static/charm-overrides/dex-auth.yaml` for a real example (`provides.dex-oidc-config: cyclic: true`).

### Multi-track criteria with any_of

```yaml
criteria:
  - any_of:
      - track: '1.24'
      - track: '1.25'
      - track: '1.26'
```

### Endpoint renames across tracks

When an endpoint was renamed between track generations (e.g. `kratos-endpoint-info`
renamed to `kratos-info`), use separate criteria blocks - one per name. Do not try to
reference the old name in the new-name block; the endpoint simply does not exist there.

```yaml
overrides:
  # old tracks: endpoint has the legacy name
  - criteria:
      - any_of:
          - track: '0.1'
          - track: '0.2'
    requires:
      old-endpoint-name:
        optional: false  # same semantic requirement, just old name
  # new tracks: endpoint has the current name
  - requires:
      new-endpoint-name:
        optional: false
```

### Track version matching (CK subordinates)

```yaml
constraints:
  - 'tracks(charms(endpoint[container-runtime])) <= tracks({self})'
  - 'tracks(charms(endpoint[cni])) <= tracks({self})'
```

### Implication (conditional requirement)

```yaml
constraints:
  - 'bool(endpoint[vault-pki]) => bool(endpoint[tls-certificates-pki])'
  - '"tls" in features(endpoint[oauth]) => reachable(endpoint[receive-ca-cert]) >= charms(endpoint[oauth])'
```

### TLS feature gating

```yaml
provides:
  ingress:
    optional: true
    features: [tls]
constraints:
  - '"tls" in features(endpoint[ingress]) => bool(endpoint[certificates])'
```

For the full DSL reference, see `docs/reference/constraint-dsl.rst`.

---

## File Format

Location: `static/charm-overrides/<charmhub-slug>.yaml`

Header comment template:

```yaml
---
# Source: https://github.com/<org>/<repo>
#
# <One sentence: why criteria blocks are needed, e.g. "tracks 0.1/0.2 predate X endpoint">
#
# <endpoint> (<direction>): REQUIRED - <one-line source evidence, e.g. "BlockedStatus in _update_workload">
# <endpoint> (<direction>): optional - <one-line reason, e.g. "observability hook, not primary purpose">
# <endpoint> (<direction>): cyclic: true - <one-line reason for cycle>
overrides:
  ...
```

**Comment budget - this is a hard limit, not a suggestion:**

- File header: at most 3-4 lines total (source link + one sentence on why criteria blocks
  exist). Do not add a running narrative of migration history, revision numbers, or dates.
- Per criteria block: at most 1-2 lines explaining what generation it covers and why the
  boundary is where it is. Do not restate evidence already given elsewhere in the file.
- Per endpoint: exactly one line, inline after the key. Not a paragraph, not a bullet list
  of supporting facts, not a quote from source code.

The evidence only needs to be *locatable*, not reproduced in full. Cite the file/function
(`BlockedStatus in charm.py::_on_config_changed`) - do not paste the surrounding code, the
revision numbers you checked, or a chronology of what changed when. A future maintainer who
wants the full story can re-run the same Charmhub/source lookup you did; the comment's job is
to point them in the right direction, not to be a standalone investigation report.

**Bad (too long - do not do this):**

```yaml
      ceph-client:
        optional: true
        # DNSaaS (designate) integration is additive, not neutron-api's
        # primary purpose. hooks/neutron_api_utils.py REQUIRED_INTERFACES
        # only lists shared-db/amqp/identity-service; external-dns is only
        # consulted via check_optional_relations() when relation_ids(
        # 'external-dns') is already non-empty, and get_optional_interfaces()
        # confirms it is not part of the mandatory set. No charm in the
        # solver's search space currently provides the "designate" interface,
        # which was causing UncompletableBundleError on the
        # etcd:proxy/etcd-proxy/neutron-api:etcd-proxy test plan.
        # Source: https://github.com/openstack/charm-neutron-api
```

**Good (one line, locatable evidence):**

```yaml
      ceph-client:
        optional: true  # not in REQUIRED_INTERFACES; neutron_api_utils.py
```

If a decision genuinely needs more than one line to justify (e.g. a subtle Meaning 2
constraint), put the longer reasoning in the PR description, not the file. The file should
stay skimmable end-to-end in under a minute.

---

## Running the Bundle Builder

The bundle builder resolves a `spec.yaml` into per-model bundle YAMLs using Charmhub
metadata and the overrides in `static/charm-overrides/`.

### Minimal spec for a single charm

```yaml
---
models:
  - name: target-model
    platform: kubernetes
    applications:
      target:
        charm: <charm-name>
        channel: "<track>/stable"
```

Run:

```bash
poetry run bundle-builder-x --spec /tmp/test.yaml --overrides ./static/charm-overrides/ --output-mermaid /tmp/result.md
```

**Important:** You do not need to add the charm's known dependencies to the spec. If an
endpoint is marked `optional: false`, the solver will discover providers from Charmhub
automatically. A single-charm spec is the correct way to test; adding explicit dependencies
would hide incorrect optional markings.

### Useful flags

| Flag | Purpose |
|------|---------|
| `--spec <path>` | Path to the spec YAML (required) |
| `--overrides <dir>` | Override directory |
| `--output-mermaid <file>` | Write a Mermaid diagram of the solution |
| `--log-level DEBUG` | Verbose; shows per-endpoint decisions |

### Evaluating the bundle output

Ask yourself: "does this deployment actually do anything useful?" That is the test, not whether relations are present.

Some charms are genuinely standalone - LXD, for example, needs no relations to be functional. A lone-charm bundle is correct for those.

For most charms, a lone-charm bundle is a red flag. If the charm needs a database, identity provider, or ingress to serve its purpose, and the bundle contains none of those, the required endpoints were probably incorrectly marked optional and the solver found a trivially minimal solution.

If the bundle pulls in something unexpected, check whether that endpoint should be `optional: true` - and if so, whether that is Meaning 1 or Meaning 2.

---

## Common Mistakes (Avoid These)

1. **Marking a primary-purpose endpoint optional upstream.** "Reaches active without it" is necessary but not sufficient. Ask: "would an operator legitimately deploy this charm and never connect this endpoint?" If no, it is not Meaning 1. Examples of always-required endpoints: etcd's `db`, postgresql's `database`, microceph's `ceph`, jimm's `oauth` and `openfga`.

2. **Marking a required endpoint optional to suppress solver sprawl.** "If I mark this required, the solver will pull in dependencies I didn't ask for" is not a valid reason for `optional: true`. The solver adding those dependencies is the correct signal: the spec is incomplete. Primary endpoints must stay required.

3. **Using circular reasoning as evidence.** "Charmhub metadata says optional=False" is not evidence for why something is required. Charmhub metadata is what you are investigating. Check the source code.

4. **Forgetting the subtle middle case.** A charm that reaches `active` but serves no purpose without at least one of N endpoints is NOT the same as "both endpoints are required". It is a Meaning 2 case: mark both optional, add an `or` constraint. Do not mark both `optional: false` - that forces the solver to always include both, which may be incorrect.

5. **Confusing cyclic with optional.** A cyclic mutual dependency (A requires from B, B requires from A) does not mean either endpoint is optional. Use `cyclic: true` to let the solver permit the cycle. Use `optional: true` only if the endpoint truly is optional. Both can be set together if needed (e.g. an endpoint that is cyclic AND only present in newer tracks).

6. **Deleting deprecated endpoints instead of marking optional.** Deprecated endpoints must remain in metadata for upgrade compatibility. Keep them, add `optional: true`.

7. **Forgetting first-match semantics.** Criteria blocks are evaluated top-to-bottom; only the first matching block applies. Put specific tracks before the fallback (no-criteria) block.

8. **Confusing scope:container endpoints.** Subordinate attachment points are always required. Never mark them optional.

9. **Writing evidence-free overrides.** If a future maintainer cannot verify why an endpoint is marked required or optional by reading the comment, the override is incomplete.

10. **Writing evidence-bloated overrides.** A multi-line comment quoting source code, listing every revision number checked, or narrating the investigation is just as much a maintenance problem as no comment at all - it goes stale and nobody rereads it. One line, a pointer to the file/function, done. See the Comment Budget rule under "File Format".

11. **Skipping a resource kind to silence a genuine leak.** `resource_tracking.skip` is only for drift the charm causes by design. A real leak firing the tracker is the correct signal; fix the charm, do not skip.

12. **Skipping too broadly.** Putting the skip in a shared/fallback block, or skipping a kind that is still consistent, hides future regressions. Scope it to the version blocks that actually exhibit the behaviour, and only the kind that drifts.

13. **Treating a bare reproduction as justification.** Reproducing the discrepancy proves it happens, not that it is expected. A skip still needs by-design evidence (source/docs/issue).

14. **Omitting the resource check test from the `-k` selection.** `test_resource_consistency_report` is the test that computes the discrepancies, and it is unmarked, so pytest deselects it whenever `-k` does not name it - leaving the run green because nothing checked. It is a required test: always include `test_resource_consistency_report` in the `-k` expression for both the without-skip and with-skip runs.

---

## Machine vs. K8s Guidance

**K8s charms:**
- Observability endpoints (metrics-endpoint, grafana-dashboard, tracing, logging) are almost always optional
- TLS pattern: `ingress` has `features: [tls]`, constraint gates `certificates` on that feature
- Check `assumes:` for overly-restrictive Juju version blocks; use the `assumes:` override if needed

**Machine charms (including Charmed Kubernetes):**
- CK charms span many tracks (1.23-1.35); use separate criteria blocks per endpoint generation
- Reactive research: `@when_not` + `status.blocked()` means required; only `@when` means optional
- Subordinate principal-attachment endpoint (container scope) is always required
- Version-track constraints are common for CNI, container-runtime, etcd
