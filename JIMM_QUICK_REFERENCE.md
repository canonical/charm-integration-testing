# juju-jimm-k8s Certificate Constraints: Quick Reference

## The Big Picture

juju-jimm-k8s needs to:
1. **Get the root CA certificate** (directly from self-signed-certificates)
2. **Get certificates through OAuth** (from hydra, which got them through traefik from self-signed-certificates)
3. **Validate** that both sources originate from the same root

---

## The Three Validation Stages

### Stage 1: Structural Validation (Scriptlet)

**Question**: Do the required endpoints exist?

```
✓ receive-ca-cert endpoint exists
✓ receive-ca-cert connects to self-signed-certificates
✓ receive-ca-cert has exactly 1 relation
✓ oauth endpoint exists
✓ oauth connects to hydra
✓ oauth has exactly 1 relation
```

**API**:
```python
event.relations.get('receive-ca-cert')       # Get relations on endpoint
event.get_relation_remote_app(endpoint, idx)  # Get provider app name
```

---

### Stage 2: Topological Validation (Z3 Solver)

**Question**: Can certificates flow through the chain?

```
Z3 models:
- app_self_signed_certificates (Bool)
- app_traefik_k8s (Bool)
- app_hydra (Bool)
- app_juju_jimm_k8s (Bool)

- integration_ssc_traefik (Bool)
- integration_traefik_hydra (Bool)
- integration_hydra_jimm (Bool)

Constraints:
1. juju_jimm must be included
2. If hydra is included, traefik must be included (transitive)
3. If traefik is included, ssc must be included (transitive)
4. Integrations imply both apps are included
5. If integrations exist, endpoints match
```

**Result**: Z3 ensures bundle topology can satisfy certificate flow.

---

### Stage 3: Runtime Validation (Scriptlet Post-Topology)

**Question**: Does the actual topology satisfy all constraints?

```python
# Trace the certificate capability from root to consumer
chain_exists = event.trace_capability_chain(
    source_app='self-signed-certificates',
    source_endpoint='certificates',
    target_app='juju-jimm-k8s',
    target_endpoint='oauth',
    hops=[
        {'app': 'traefik-k8s', 'endpoint': 'ingress'},
        {'app': 'hydra', 'endpoint': 'oauth'}
    ]
)

# Verify both paths originate from same provider
ca_provider = event.get_relation_remote_app('receive-ca-cert', 0)
chain_root = event.trace_chain_source('oauth', 'certificates')

if ca_provider != chain_root:
    event.reject('provider_mismatch')
```

---

## Decomposition

### Structural Layer (Endpoint Presence)
**Constraints**: Required, Limit, Mutual Exclusion

```
receive-ca-cert: REQUIRED
- Type: Required endpoint constraint (#1)
- Cardinality: Exactly 1
- Provider: Must be self-signed-certificates

oauth: REQUIRED
- Type: Required endpoint constraint (#1)
- Cardinality: Exactly 1
- Provider: Must be hydra
```

### Topological Layer (Chain Existence)
**Constraints**: Transitive Capability, Same-Application Mandate

```
Certificate Chain: TRANSITIVE CAPABILITY (#5)
- Path: self-signed-certificates → traefik-k8s → hydra → juju-jimm-k8s
- Capability: certificates
- Bridge endpoints: (certs→ingress), (ingress→oauth), (oauth→receive-ca-cert)

Provider Consistency: SAME-APPLICATION MANDATE (#7)
- CA provider (receive-ca-cert) == Chain root (oauth path)
- Both must point to self-signed-certificates
```

### Cryptographic Layer (Signature Validation)
**Constraints**: Custom validation (Pre-deployment check)

```
Certificate Signature: RUNTIME VALIDATION
- CA cert source: receive-ca-cert relation
- OAuth certs: oauth relation
- Validation: All oauth certs must be signed by CA cert
- Failure: Reject deployment
```

---

## Z3 Formalization

### Variables

```python
# Apps
app_ssc = Bool("app_self_signed_certificates")
app_traefik = Bool("app_traefik_k8s")
app_hydra = Bool("app_hydra")
app_jimm = Bool("app_juju_jimm_k8s")

# Integrations
int_ssc_traefik = Bool("int_ssc_traefik_certs")
int_traefik_hydra = Bool("int_traefik_hydra_ingress")
int_hydra_jimm = Bool("int_hydra_jimm_oauth")
int_ssc_jimm_ca = Bool("int_ssc_jimm_ca_cert")

# Capabilities
has_jimm_certs = Bool("has_jimm_certs")  # Are jimm's certificate requirements satisfied?
```

### Constraints

```python
# 1. jimm is required
solver.add(app_jimm == True)

# 2. If jimm is included, CA integration is required
solver.add(Implies(app_jimm, int_ssc_jimm_ca))

# 3. If jimm has CA integration, ssc must be included
solver.add(Implies(int_ssc_jimm_ca, app_ssc))

# 4. If jimm has oauth, hydra must be included
solver.add(Implies(int_hydra_jimm, app_hydra))

# 5. If hydra is included, traefik must be included (for cert chain)
solver.add(Implies(app_hydra, app_traefik))

# 6. If traefik is included, ssc must be included (cert source)
solver.add(Implies(app_traefik, app_ssc))

# 7. Integrations require both apps
solver.add(Implies(int_ssc_traefik, And(app_ssc, app_traefik)))
solver.add(Implies(int_traefik_hydra, And(app_traefik, app_hydra)))
solver.add(Implies(int_hydra_jimm, And(app_hydra, app_jimm)))

# 8. Endpoint endpoints must match
solver.add(Implies(int_ssc_traefik, And(
    provides(app_ssc, 'certificates'),
    requires(app_traefik, 'certificates')
)))
solver.add(Implies(int_traefik_hydra, And(
    provides(app_traefik, 'ingress'),
    requires(app_hydra, 'public-ingress')
)))
solver.add(Implies(int_hydra_jimm, And(
    provides(app_hydra, 'oauth'),
    requires(app_jimm, 'oauth')
)))

# 9. Certificate requirements satisfied if both paths exist
solver.add(Implies(
    And(int_ssc_jimm_ca, int_ssc_traefik, int_traefik_hydra, int_hydra_jimm),
    has_jimm_certs
))

# 10. jimm certificate requirements must be satisfied
solver.add(has_jimm_certs == True)
```

---

## Scriptlet API Needed

```python
# Discovery
event.relations.get(endpoint_name: str) -> List[Relation]
event.get_relation_remote_app(endpoint_name: str, index: int) -> str

# Topology
event.trace_capability_chain(
    source_app: str,
    source_endpoint: str,
    target_app: str,
    target_endpoint: str,
    hops: List[Dict]
) -> bool

event.trace_chain_source(endpoint_name: str, capability: str) -> str

# Validation
event.reject(constraint_type: str, details: str) -> NoReturn

# Runtime (if implementing cryptographic validation)
event.get_relation_data(endpoint_name: str, index: int) -> Dict
```

---

## Implementation Checklist

- [x] Scriptlet structural validation (endpoints, cardinality, providers)
- [x] Z3 model for bundle topology
- [x] Scriptlet post-topology validation (chain tracing, provider consistency)
- [ ] Scriptlet cryptographic validation (certificate signature checking)
- [ ] Bundle builder integration with post-topology validator
- [ ] Test scenarios:
  - [ ] Valid bundle (all constraints satisfied)
  - [ ] Missing receive-ca-cert (fails constraint #1)
  - [ ] Wrong CA provider (fails constraint #7)
  - [ ] Broken chain (traefik missing, fails constraint #5)
  - [ ] Invalid certificate signature (fails runtime validation)
