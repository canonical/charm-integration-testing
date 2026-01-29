# juju-jimm-k8s Certificate Constraints: Detailed Breakdown

## Context

juju-jimm-k8s is a charm that manages Juju controllers. It requires:

1. Certificates from self-signed-certificates (directly, via `receive-ca-cert` endpoint)
2. OAuth functionality from hydra (via `oauth` endpoint)
3. The certificates it receives via oauth must be traceable back to a trusted root CA

---

## Problem Statement

**What needs validation?**

juju-jimm-k8s must ensure that:

1. It has a **direct integration** with self-signed-certificates (to get the root CA certificate)
2. It has an **indirect path** to certificates via: self-signed-certificates → traefik → hydra → juju-jimm
3. The **certificate chain** is valid (certificates received through hydra are signed by the CA from self-signed-certificates)
4. It can **verify** the certificates from hydra using the root CA

**What constraints exist?**

- **Transitive capability requirement** (#5): Certificates must flow through the integration chain
- **Mandatory direct relationship** (#7): Must have direct integration with root certificate provider
- **Certificate validation** (custom): Received certificates must match the root CA

---

## How juju-jimm-k8s Would Validate This

### 1. Declare Certificate Constraints in Scriptlet

```python
# juju-jimm-k8s.star

def init():
    juju.observe("validate", on_validate)
    juju.observe("topology_ready", on_topology_ready)

def on_validate(event):
    """
    Validate that juju-jimm-k8s has proper certificate integrations.
    """
    
    # CONSTRAINT 1: Direct integration with self-signed-certificates required
    ca_cert_relations = event.relations.get('receive-ca-cert', [])
    if len(ca_cert_relations) == 0:
        event.reject(
            'missing_required_integration',
            'receive-ca-cert endpoint must be integrated with self-signed-certificates'
        )
    if len(ca_cert_relations) > 1:
        event.reject(
            'limit',
            'receive-ca-cert:1 (too many CA cert providers)'
        )
    
    # CONSTRAINT 2: OAuth integration required (for hydra)
    oauth_relations = event.relations.get('oauth', [])
    if len(oauth_relations) == 0:
        event.reject(
            'missing_required_integration',
            'oauth endpoint must be integrated with hydra'
        )
    
    # CONSTRAINT 3: Verify certificate chain exists
    # This validation checks if the topology has:
    #   self-signed-certificates → traefik → hydra → juju-jimm
    has_cert_chain = event.check_capability_chain(
        source='self-signed-certificates',
        target='juju-jimm-k8s',
        endpoint_chain=['certificates', 'ingress', 'oauth'],
        capability='certificates'
    )
    if not has_cert_chain:
        event.reject(
            'broken_certificate_chain',
            'Certificate chain is broken: self-signed-certificates → traefik → hydra → juju-jimm'
        )
    
    # CONSTRAINT 4: Verify self-signed-certificates is the same application
    # as the one providing CA cert
    ca_provider = ca_cert_relations[0]['remote_app']  # Should be 'self-signed-certificates'
    cert_chain_root = event.get_capability_source('oauth', 'certificates')
    
    if ca_provider != cert_chain_root:
        event.reject(
            'certificate_mismatch',
            f'CA cert provider ({ca_provider}) differs from certificate chain root ({cert_chain_root})'
        )

def on_topology_ready(event):
    """
    Once topology is ready, we can validate runtime constraints.
    """
    
    # Extract certificates from both paths
    ca_cert = event.get_relation_data('receive-ca-cert')[0]['data']['certificate']
    oauth_certs = event.get_relation_data('oauth')[0]['data']['certificates']
    
    # VALIDATION: Verify certificates are signed by the CA
    for cert in oauth_certs:
        if not verify_certificate_chain(cert, ca_cert):
            event.reject(
                'certificate_validation_failed',
                f'Certificate from oauth path is not signed by root CA'
            )
```

---

## Breaking Down the Constraints

### Constraint 1: Direct Integration (receive-ca-cert)

**What it is:**

- A mandatory direct relationship between juju-jimm-k8s and self-signed-certificates
- Endpoint: `receive-ca-cert` on juju-jimm-k8s

**Why?**

- juju-jimm-k8s needs the root CA certificate to verify all downstream certificates
- This cannot be satisfied indirectly through other charms
- It's a **bootstrap requirement**

**Z3 Model:**

```
has_receive_ca_cert = Bool("jimm_has_receive_ca_cert")
ca_provider = String("jimm_ca_provider")

# Constraint: juju-jimm requires receive-ca-cert
solver.add(has_receive_ca_cert == True)

# Constraint: receive-ca-cert must connect to self-signed-certificates
solver.add(Implies(has_receive_ca_cert, ca_provider == "self-signed-certificates"))

# Constraint: receive-ca-cert endpoint can have at most 1 relation
solver.add(receive_ca_cert_relations <= 1)
```

---

### Constraint 2: OAuth Integration (oauth)

**What it is:**

- A required integration with hydra via the `oauth` endpoint
- This is a standard integration dependency

**Why?**

- juju-jimm-k8s needs OAuth for authentication
- It doesn't directly provide certificates, but hydra will include certificates in the oauth relation

**Z3 Model:**

```
has_oauth = Bool("jimm_has_oauth")
oauth_provider = String("jimm_oauth_provider")

# Constraint: juju-jimm requires oauth
solver.add(has_oauth == True)

# Constraint: oauth must connect to hydra
solver.add(Implies(has_oauth, oauth_provider == "hydra"))
```

---

### Constraint 3: Certificate Chain (Transitive Capability)

**What it is:**

- A chain of integrations that deliver certificates from root to jimm:
  - self-signed-certificates → (certificates) → traefik-k8s
  - traefik-k8s → (ingress) → hydra
  - hydra → (oauth) → juju-jimm-k8s

**Why?**

- The certificates jimm receives via oauth must come from self-signed-certificates
- This is a **transitive path** that enables capability propagation

**Z3 Model:**

```
# Bridge from traefik to hydra (traefik bridges certificates to hydra)
has_traefik_hydra_bridge = Bool("traefik_hydra_bridge")

# Bridge from hydra to jimm (hydra bridges certificates to jimm)
has_hydra_jimm_bridge = Bool("hydra_jimm_bridge")

# Root integration
has_ssc_traefik = Bool("ssc_traefik_certs")

# Chain constraint: if all integrations exist, chain is satisfied
solver.add(Implies(
    And(has_ssc_traefik, has_traefik_hydra_bridge, has_hydra_jimm_bridge),
    certificate_chain_satisfied == True
))

# Additionally, the provider at each step must be correct:
solver.add(Implies(has_ssc_traefik, traefik_cert_provider == "self-signed-certificates"))
solver.add(Implies(has_traefik_hydra_bridge, hydra_ingress_provider == "traefik-k8s"))
solver.add(Implies(has_hydra_jimm_bridge, jimm_oauth_provider == "hydra"))
```

---

### Constraint 4: Certificate Validation (Runtime)

**What it is:**

- Runtime verification that certificates from oauth are signed by the root CA

**Why?**

- Cryptographic validation ensures the chain is authentic
- This catches misconfiguration or security issues at deploy time

**Z3 Model (abstract):**

```
# Certificate objects (symbolic)
root_ca = Object("root_ca")
oauth_certs = Array(Object)

# Constraint: all oauth certs must be signed by root CA
for i in oauth_certs:
    solver.add(is_signed_by(oauth_certs[i], root_ca) == True)
```

---

## Implementation Strategy

### Stage 1: Declarative Validation (Scriptlet)

The scriptlet validates:

1. ✅ Presence of required endpoints (receive-ca-cert, oauth)
2. ✅ Presence of certificate chain (topology-level check)
3. ✅ Consistency between CA provider and chain root
4. ⏳ Certificate signatures (requires data exchange)

### Stage 2: Z3 Solving

Z3 ensures:

1. ✅ All endpoints can be satisfied by available charms
2. ✅ Certificate chain exists with correct providers
3. ✅ No conflicts between constraints
4. ✅ Minimal satisfying bundle

### Stage 3: Runtime Validation

At deploy time:

1. ✅ Certificates are fetched from both paths
2. ✅ Cryptographic validation is performed
3. ✅ If validation fails, deployment is rejected

---

## Scriptlet API Needed

```python
# Relations API
event.relations.get(endpoint_name)  # Get list of relations
event.check_limit(endpoint_name, max_count)  # Validate cardinality

# Topology API
event.check_capability_chain(source, target, endpoint_chain, capability)
event.get_capability_source(endpoint_name, capability)
event.get_relation_data(endpoint_name)

# Rejection API
event.reject(constraint_type, details)

# Bridge API (for advanced constraints)
event.declare_bridge(
    from_endpoint='oauth',
    to_endpoint='receive-ca-cert',
    bridge_capability='certificates',
    validation_fn=verify_certificate_chain
)
```

---

## Key Insights

1. **Certificate constraints are multi-faceted:**
   - Structural (endpoint presence, cardinality)
   - Topological (chain existence, provider consistency)
   - Cryptographic (signature validation)

2. **juju-jimm-k8s needs both direct and indirect paths:**
   - Direct: Bootstraps trust (root CA)
   - Indirect: Receives identity certificates

3. **Z3 handles structural and topological validation:**
   - Can model provider chains
   - Can verify endpoint consistency
   - Cannot perform cryptographic verification

4. **Scriptlet bridges declarative and runtime validation:**
   - Declares constraints (Starlark code)
   - Performs topological checks (pre-deploy)
   - Validates certificates (runtime, if needed)

5. **This is Constraint #5 + #7 combined:**
   - #5: Transitive capability (certificates flow through chain)
   - #7: Same-application mandate (must go through specific provider)
   - Together: "I need X to flow transitively, but must come from Z"
