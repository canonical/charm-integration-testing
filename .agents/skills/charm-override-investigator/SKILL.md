---
name: charm-override-investigator
description: 'Charm Override Investigator - investigates charm metadata and creates per-charm override YAML files in static/charm-overrides/. USE FOR: creating a new override from scratch, updating after an upstream PR lands, fixing broken constraints, auditing against live metadata. GUIDED WORKFLOWS: write_new_override, update_existing_override, validate_override. INPUTS: charm name, optional upstream PR URL, optional track filter. OUTPUTS: PR-ready YAML with evidence comments.'
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
7. Add evidence comments for every non-obvious `optional: false` AND `optional: true` decision. The stated rule is that both directions require justification; the comment only needs to be one line but must cite source code, docs, or a domain expert statement.
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

---

## Self-Validation Checklist

1. **YAML structure** - parses correctly; matches `CharmGlobalOverrides` schema
2. **Lint** - `poetry run yamlfix --check static/charm-overrides/<charm>.yaml` passes
3. **Endpoint names** - every name exists in the charm's actual Charmhub metadata on the correct side (requires/provides)
4. **Config keys** - every key in `configs:` exists in the charm's `config.yaml`
5. **Criteria coverage** - every criteria block matches at least one published channel
6. **Evidence present** - every `optional: false` has a source comment; every `optional: true` on a non-obvious endpoint has a reason
7. **No Meaning 2 misuse** - no endpoint marked `optional: true` solely to suppress solver sprawl
8. **Realistic bundle** - solver run produces a bundle you would actually deploy (not a lone charm, not missing obvious dependencies)
9. **Cross-reference** - compare patterns with similar overrides in `static/charm-overrides/`

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

Keep comments short. One line per notable endpoint decision. Full prose is not needed; just
enough for a reviewer to verify the claim without re-reading all the source code.

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

**Charms that do not support our testing infrastructure:**
- `listed: false` alone only hides a charm from being picked as an *incidental neighbor* in
  unrelated bundles - it does not stop the charm from being built directly (e.g. as the
  target of its own `test_build_bundle`).
- If a charm's own mandatory (non-optional) endpoints can only be fulfilled by other
  charms that are also excluded (e.g. the entire OpenStack family's `identity-service`/`ha`
  endpoints, which only keystone/hacluster provide, and both are delisted under SQT-1081),
  building it directly will still fail deep inside relation resolution with a confusing
  "Cannot fulfill charm endpoints" error.
- The correct fix is not to special-case the delisting filter. Add `assumes: [openstack-unsupported]`
  (or another descriptive sentinel feature name) to every criteria block in that charm's
  overrides. `_ensure_compatibility()` never supplies custom sentinel features (only `juju`
  and `k8s-api` are ever satisfied), so `charm_from_store()` fails immediately and clearly
  whenever the charm is requested directly, and is silently skipped when only being
  considered as a candidate neighbor. See issue #813.
