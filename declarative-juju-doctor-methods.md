# Juju Doctor Rulesets

This is what a ruleset looks like:

```yaml
name: RuleSet - test all
probes:
  - name: Probe - test passing
    type: scriptlet
    url: file://tests/resources/probes/python/passing.py
  - type: ruleset
    url: file://tests/resources/probes/ruleset/small-dir
  - type: ruleset
    url: file://tests/resources/probes/ruleset/scriptlets.yaml
  - type: ruleset
    url: file://tests/resources/probes/ruleset/builtins.yaml
  - name: Builtin application-exists
    type: builtin/application-exists
    with:
      - application-name: catalogue
  - name: Builtin app-relation-exists
    type: builtin/app-relation-exists
    with:
      - apps: [grafana:catalogue, catalogue:catalogue]
  - name: Builtin offer-exists
    type: builtin/offer-exists
    with:
      - offer-name: loki-logging
        endpoint: logging
        interface: loki_push_api
```

The problem is the python probes. Those are not easily usable because they make it *very* hard

# Options

## Option 1: Yaml DSL

```yaml
name: canonical-livepatch-server-k8s
probes:
  - name: At least one database endpoint
    type: file://integration-topology.py
    with:
      charm: canonical-livepatch-server-k8s
      equal:
        - sum:
            - count: database
            - count: database-legacy
        - 1
```

## Option 2: SMT Strings

```yaml
name: canonical-livepatch-server-k8s
probes:
  - name: At least one database endpoint
    type: file://integration-topology.py
    with:
      assert: '(= (+ database-legacy_count database_count) 1)'
```

## Option 3: Flat predicate expressions

```yaml
name: canonical-livepatch-server-k8s
probes:
  - name: Exactly one database endpoint
    type: file://integration-topology.py
    with:
      charm: canonical-livepatch-server-k8s
      assert: 'count(database) + count(database-legacy) == 1'

  - name: certificates required when tls database endpoint is connected
    type: file://integration-topology.py
    with:
      charm: canonical-livepatch-server-k8s
      assert: 'connected(database) implies connected(certificates)'
```

Plain Python-style expression strings. A small set of built-in functions (`count`, `connected`, `implies`) are evaluated against the model — as real integers/bools for juju doctor live-checks, or as Z3 expressions for bundle builder solving. One format, two backends, no recursive YAML tree.

# Handle categorically different constraints

## Acyclic

By default, the bundle builder will enforce that all integrations are acyclic. Can a juju probe add an exception? Does that even make sense or is feasible? This was previous done with a field next to optional and limit in the metadata called cyclic.

## Transitive capabilities

If a charm said that "hey, I can provide certificates, it is through the provider of this other charm!". How would that work?

## Other

Right now the integration-topology.py performs actions on one charm. Could it be expanded to instead operate on a bundle? How would that work? Perhaps a function like `charms(canonical-livepatch-server-k8s)` and now you are operating on a set of "applications". Thoughts?

---

# DSL Spec (Option 3)

## Probe type

```yaml
- name: <human-readable description>
  type: builtin/integration-topology
  with:
    charm: <charm-name>   # optional; omit for bundle-scoped assertions
    assert: '<expression>'
```

## Scoping

- **Charm-scoped** (`charm:` present): functions operate on the named charm's endpoints.
  The charm may appear multiple times in the bundle (multiple application instances); the
  assertion must hold for every instance.
- **Bundle-scoped** (`charm:` absent): functions operate across the entire bundle.

## Expression syntax

Plain Python-style expressions. Operators: `+`, `-`, `*`, `==`, `!=`, `<`, `<=`, `>`, `>=`,
`and`, `or`, `not`. The keyword `implies` is sugar for `not A or B`.

## Built-in functions

### Local (charm-scoped)

| Function | Returns | Description |
|---|---|---|
| `count(endpoint)` | `int` | Number of active integrations on this endpoint |
| `connected(endpoint)` | `bool` | True if the endpoint has at least one integration (`count > 0`) |
| `apps(endpoint)` | `set[app]` | The set of applications connected on this endpoint (regardless of direction) |

### Traversal

| Function | Returns | Description |
|---|---|---|
| `transitive(endpoint, interface)` | `set[app]` | For each app connected on this endpoint, follow integrations of the given interface upstream until reaching terminal providers (those with no further integration of that interface). Returns the set of all such terminal apps. |

When a chain is unambiguous (single provider at each hop), the set has one element. Equality between two `transitive()` calls asserts the sets are identical.

### Bundle-scoped

| Function | Returns | Description |
|---|---|---|
| `consumers(app, endpoint)` | `set[app]` | All applications connected to the given endpoint of the given app |

### Set operations

`==`, `!=`, `<=` (subset), `&` (intersection), `|` (union).

## Evaluation backends

The same expression is valid in two contexts:

- **Live-check** (juju doctor against a real model): functions return concrete Python sets of
  application name strings. Assertion failure raises an exception with the expression and
  actual values.
- **Solver** (bundle builder, Z3): functions return Z3 set expressions. The solver finds a
  bundle assignment satisfying all assertions simultaneously, or proves no such bundle exists.

## Examples

```yaml
# Mutual exclusion
assert: 'count(database) + count(database-legacy) == 1'

# Conditionally required
assert: 'connected(vault-pki) implies connected(tls-certificates-pki)'

# Min observability
assert: 'count(metrics-endpoint) + count(logging-provider) + count(tracing) >= 1'

# Capability requirement (specific provider charm name)
# hydra
assert: '(feature(ingress) == "tls") implies (feature(oauth) == "tls")'
# traefik
assert: 'connected(receive-ca-cert) implies (feature(ingress) == "tls")'

# Consistent TLS trust chain (hydra): root CA reachable through ingress == root CA reachable through oauth consumers
assert: 'transitive(public-ingress, "tls-certificates") == transitive(oauth, "tls-certificates")'

# Trust chain consumer (juju-jimm-k8s): direct CA provider must match root of oauth chain
assert: 'apps(receive-ca-cert) == transitive(oauth, "tls-certificates")'

# Same application (mongodb-k8s): ldap and ldap-certificate-transfer must connect to the same app
assert: 'apps(ldap) == apps(ldap-certificate-transfer)'
```

## Out of scope

- **Acyclic constraint**: a global graph property, not expressible per-charm. Enforced
  unconditionally by the bundle builder; overridable via a `cyclic: true` field in
  `metadata.yaml`, not via probes.
- **Version compatibility**: requires access to channel/revision metadata, not just
  integration topology. Needs a separate probe type (`builtin/version-compatibility`).