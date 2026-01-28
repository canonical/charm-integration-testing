# Constraint Types and Z3 Encoding Patterns

This document explains how each type of bundle building constraint maps to Z3.

## Core Variables

```python
# Boolean for each potential application in bundle
app_X = Bool('app_charm_name')

# Boolean for each potential integration
# Naming: int_<provider>_<provider_endpoint>__<consumer>_<consumer_endpoint>
int_postgres_db__app_database = Bool('int_...')

# Boolean for each endpoint being "integrated" (has at least one connection)
ep_app_endpoint = Bool('ep_app_endpoint')
```

## Constraint Type 1: Interface Matching

**Rule**: Integration can only happen if interfaces match.

**Natural Language**: "A `postgresql_client` requires endpoint can only integrate with a `postgresql_client` provides endpoint."

**Z3 Encoding**:
```python
# Option A: Implicit (by only creating integration variables for valid pairs)
# Only define: int_postgres_db__jimm_db if interfaces match

# Option B: Explicit constraint
interface_matches = Bool('interface_matches_postgres_jimm')
solver.add(interface_matches == (interface_postgres == interface_jimm))
solver.add(Implies(int_postgres_jimm_db, interface_matches))
```

**In Practice**: Usually implicit—we only create integration variables for compatible interfaces.

---

## Constraint Type 2: Directionality (Provides → Requires)

**Rule**: Integrations go from `provides` to `requires`, not reversed.

**Natural Language**: "PostgreSQL provides database; jimm requires database. Integration can only go postgresql → jimm."

**Z3 Encoding**:
```python
# Implicit in variable naming
int_postgres_provides__jimm_requires = Bool('...')  # OK
# We don't create: int_jimm_requires__postgres_provides  # Invalid direction
```

**In Practice**: Implicit—only create variables for valid directions.

---

## Constraint Type 3: Required Endpoints

**Rule**: If an app is in the bundle, certain endpoints MUST be integrated.

**Natural Language**: "If jimm is deployed, jimm.database must be integrated."

**Z3 Encoding**:
```python
# ep_jimm_database is true iff at least one integration uses it
solver.add(Implies(app_jimm, ep_jimm_database))
```

---

## Constraint Type 4: Mutually Exclusive Endpoints (XOR)

**Rule**: Exactly one (or at most one) of a set of endpoints can be integrated.

**Natural Language**: "jimm must use exactly one of: oauth OR oauth-tls."

**Z3 Encoding**:
```python
# Exactly one (XOR with at-least-one):
solver.add(PbEq([(ep_jimm_oauth, 1), (ep_jimm_oauth_tls, 1)], 1))

# At most one (XOR without requirement):
solver.add(PbLe([(ep_jimm_oauth, 1), (ep_jimm_oauth_tls, 1)], 1))

# Alternative syntax for exactly one:
solver.add(Or(ep_jimm_oauth, ep_jimm_oauth_tls))  # at least one
solver.add(Not(And(ep_jimm_oauth, ep_jimm_oauth_tls)))  # at most one
```

---

## Constraint Type 5: Conditional Requirements (If-Then)

**Rule**: If endpoint A is integrated, then endpoint B must/must not be integrated.

**Natural Language**: 
- "If jimm.oauth-tls is used, postgresql.certificates MUST be integrated."
- "If app uses legacy-mode, modern-endpoint MUST NOT be integrated."

**Z3 Encoding**:
```python
# If A then require B:
solver.add(Implies(ep_jimm_oauth_tls, ep_postgresql_certificates))

# If A then forbid B:
solver.add(Implies(ep_app_legacy, Not(ep_app_modern)))

# Complex: If any of A/B/C, then require D:
solver.add(Implies(Or(ep_A, ep_B, ep_C), ep_D))

# If A, then exactly one of B/C:
solver.add(Implies(ep_A, PbEq([(ep_B, 1), (ep_C, 1)], 1)))
```

---

## Constraint Type 6: Same Application Constraint

**Rule**: Two endpoints of an app must integrate with the SAME target application.

**Natural Language**: "If app1.endpoint_a connects to X, then app1.endpoint_b must also connect to X."

**Z3 Encoding** (requires tracking which app each endpoint connects to):
```python
# Create auxiliary variables for "which app does endpoint connect to"
# For each potential target app:
ep1_connects_to_X = Bool('ep1_connects_to_X')
ep1_connects_to_Y = Bool('ep1_connects_to_Y')
ep2_connects_to_X = Bool('ep2_connects_to_X')
ep2_connects_to_Y = Bool('ep2_connects_to_Y')

# Link to actual integrations:
solver.add(ep1_connects_to_X == int_X_ep1)
solver.add(ep1_connects_to_Y == int_Y_ep1)
solver.add(ep2_connects_to_X == int_X_ep2)
solver.add(ep2_connects_to_Y == int_Y_ep2)

# Same application constraint:
solver.add(ep1_connects_to_X == ep2_connects_to_X)
solver.add(ep1_connects_to_Y == ep2_connects_to_Y)
# (both must connect to X, or both to Y, or both to neither)
```

---

## Constraint Type 7: Channel Matching

**Rule**: Two apps can only integrate if they're on compatible channels.

**Natural Language**: "postgresql on 14/stable can integrate with jimm on any channel, but postgresql 14/edge requires jimm on edge or candidate."

**Z3 Encoding** (requires String or Int variables for channels):
```python
# Option A: Discrete channels as integers
channel_jimm = Int('channel_jimm')  # 0=stable, 1=candidate, 2=edge
channel_postgres = Int('channel_postgres')

# Compatible if same channel or specific allowed pairs
compatible = Or(
    channel_jimm == channel_postgres,
    And(channel_postgres == 0, channel_jimm >= 0),  # stable postgres works with any jimm
)
solver.add(Implies(int_postgres_jimm, compatible))

# Option B: Boolean flags per channel per app
jimm_stable = Bool('jimm_stable')
jimm_edge = Bool('jimm_edge')
postgres_stable = Bool('postgres_stable')
# Exactly one channel per app:
solver.add(PbEq([(jimm_stable, 1), (jimm_edge, 1)], 1))
# Compatible:
solver.add(Implies(int_postgres_jimm, Or(
    And(jimm_stable, postgres_stable),
    And(jimm_edge, postgres_stable),
    And(jimm_edge, jimm_edge),
)))
```

---

## Constraint Type 8: Bridges / Transitive Resource Passing

**Rule**: If app A gets resource X from app B, and B gets X from C, then A transitively gets X from C.

**Natural Language**: "Vault provides certificates. If vault gets certificates via its tls-certificates-pki endpoint from tls-operator, then apps that get certificates from vault are transitively getting them from tls-operator."

**Z3 Encoding**:
```python
# Create auxiliary variables for "app has resource"
vault_has_certs = Bool('vault_has_certificates')
tls_provides_certs = Bool('tls_provides_certificates')

# Vault has certs if it's connected to a certificate provider:
solver.add(vault_has_certs == int_tls_vault_certs_requires)

# If vault provides certs to someone, it must have certs:
solver.add(Implies(int_vault_other_certs_provides, vault_has_certs))

# Transitivity: if A gets certs from B, and B gets certs from C, track the origin
# This gets complex; simpler approach: track "has resource" per app:
postgres_has_certs = Bool('postgres_has_certificates')
solver.add(postgres_has_certs == int_tls_postgres_certs)

# If postgres needs certs for TLS, it must have them:
solver.add(Implies(postgres_uses_tls, postgres_has_certs))
```

**Advanced**: For true transitivity tracking, use auxiliary variables like `certs_from(A, C)` and rules:
```python
# certs_from(A, C) iff:
#   - direct: A integrates with C on certificates endpoint, OR
#   - transitive: A integrates with B on certs, and B integrates with C on certs
certs_from_postgres_to_tls = Bool('certs_from_postgres_tls')
certs_from_postgres_via_vault_to_tls = Bool('certs_from_postgres_vault_tls')

solver.add(certs_from_postgres_to_tls == int_tls_postgres_direct)
solver.add(certs_from_postgres_via_vault_to_tls == And(
    int_vault_postgres_certs,
    int_tls_vault_certs
))
postgres_has_certs_from_tls = Or(
    certs_from_postgres_to_tls,
    certs_from_postgres_via_vault_to_tls
)
```

---

## Constraint Type 9: Cardinality / Limits

**Rule**: An endpoint can only handle N connections.

**Natural Language**: "postgresql.database can accept at most 5 client connections."

**Z3 Encoding**:
```python
# Count integrations using this endpoint:
integrations_using_postgres_db = [
    int_postgres_jimm_db,
    int_postgres_hydra_db,
    int_postgres_other_db,
    # ... all integrations to postgres.database
]

# At most 5:
solver.add(PbLe([(i, 1) for i in integrations_using_postgres_db], 5))
```

---

## Optimization

### Minimize Number of Apps and Integrations

```python
# Weight 10 per app (apps are expensive)
for app in [app_postgres, app_hydra, app_vault, ...]:
    solver.add_soft(Not(app), weight=10, id='minimize_apps')

# Weight 1 per integration
for integration in [int_postgres_jimm, int_hydra_jimm, ...]:
    solver.add_soft(Not(integration), weight=1, id='minimize_integrations')
```

### Prioritize Certain Node Types

```python
# Prefer managed services over self-hosted:
solver.add_soft(Not(app_selfhosted_db), weight=20, id='prefer_managed')
solver.add_soft(Not(app_managed_db), weight=5, id='prefer_managed')

# Prefer simpler configurations:
solver.add_soft(Not(ep_oauth_tls), weight=3, id='prefer_simple')  # prefer plain oauth
```

### Minimize Changes to Existing Bundle

If you have an existing bundle and want minimal edits:

```python
# For each app: prefer to keep current state
for app, currently_included in existing_bundle.apps.items():
    if currently_included:
        solver.add_soft(app_var, weight=2, id='minimize_changes')
    else:
        solver.add_soft(Not(app_var), weight=2, id='minimize_changes')
```

---

## Putting It All Together

A typical Z3 bundle builder flow:

1. **Define variables**: apps, endpoints, integrations
2. **Add structural constraints**: integration ↔ both apps exist
3. **Add domain constraints**: interface matching, directionality, required endpoints
4. **Add custom rules**: XOR, if-then, same-app, channels, bridges
5. **Add soft constraints**: minimize size, prioritize types
6. **Solve**: `solver.check()`
7. **Extract solution**: read `model.evaluate(var)` for each variable
8. **Generate bundle YAML** from the true variables

This gives you a valid, minimal bundle satisfying all constraints!
