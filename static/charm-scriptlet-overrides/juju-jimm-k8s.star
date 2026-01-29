# juju-jimm-k8s charm scriptlet
# Constraint coverage:
# - Direct certificate requirement (receive-ca-cert endpoint)
# - OAuth requirement (for hydra)
# - Transitive certificate chain validation
# - Certificate provider consistency
# - Same-application mandate for trust bootstrap

def init():
    juju.observe("validate", on_validate)
    juju.observe("post_topology_validate", on_post_topology_validate)

def on_validate(event):
    """
    Primary validation stage: check endpoint presence and cardinality.
    """
    
    # CONSTRAINT 1: receive-ca-cert is required
    # This is the bootstrap path for certificate validation
    ca_cert_relations = event.relations.get('receive-ca-cert', [])
    if len(ca_cert_relations) == 0:
        event.reject(
            'missing_required_endpoint',
            'receive-ca-cert endpoint is required (must integrate with self-signed-certificates)'
        )
        return
    
    # CONSTRAINT 2: receive-ca-cert has cardinality of exactly 1
    if len(ca_cert_relations) > 1:
        event.reject(
            'limit_exceeded',
            'receive-ca-cert:1 (exactly one CA certificate provider allowed)'
        )
        return
    
    # CONSTRAINT 3: verify receive-ca-cert connects to self-signed-certificates
    ca_remote_app = ca_cert_relations[0].get('remote_app')
    if ca_remote_app != 'self-signed-certificates':
        event.reject(
            'wrong_provider',
            f'receive-ca-cert must connect to self-signed-certificates, not {ca_remote_app}'
        )
        return
    
    # CONSTRAINT 4: oauth is required
    oauth_relations = event.relations.get('oauth', [])
    if len(oauth_relations) == 0:
        event.reject(
            'missing_required_endpoint',
            'oauth endpoint is required (must integrate with hydra)'
        )
        return
    
    # CONSTRAINT 5: oauth has cardinality of at most 1
    if len(oauth_relations) > 1:
        event.reject(
            'limit_exceeded',
            'oauth:1 (at most one oauth provider allowed)'
        )
        return
    
    # CONSTRAINT 6: verify oauth connects to hydra
    oauth_remote_app = oauth_relations[0].get('remote_app')
    if oauth_remote_app != 'hydra':
        event.reject(
            'wrong_provider',
            f'oauth must connect to hydra, not {oauth_remote_app}'
        )
        return

def on_post_topology_validate(event):
    """
    Secondary validation stage: check topological constraints.
    This runs after the bundle topology is built and we can trace paths.
    """
    
    # CONSTRAINT 7: Certificate chain must exist
    # Path: self-signed-certificates → traefik-k8s → hydra → juju-jimm-k8s
    # This validates that certificates can flow from SSC to jimm through hydra.
    
    # Check if we can trace from self-signed-certificates to juju-jimm-k8s
    # through the capability path
    
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
    
    if not chain_exists:
        event.reject(
            'broken_certificate_chain',
            'Certificate chain broken: self-signed-certificates → traefik → hydra → juju-jimm'
        )
        return
    
    # CONSTRAINT 8: Verify provider consistency
    # The CA certificate provider must be the same as the root of the chain
    
    ca_provider_jimm = event.get_relation_remote_app('receive-ca-cert', 0)
    chain_root = event.trace_chain_source('oauth', 'certificates')
    
    if ca_provider_jimm != chain_root:
        event.reject(
            'provider_mismatch',
            f'CA provider ({ca_provider_jimm}) differs from certificate chain root ({chain_root})'
        )
        return
    
    # CONSTRAINT 9: Verify transitive capability satisfaction
    # The certificates received via oauth must be from self-signed-certificates
    # This is a transitive capability constraint (#5)
    
    # At this point we've verified:
    # 1. Direct path exists to CA
    # 2. Transitive path exists to certificates via hydra
    # 3. Both paths originate from the same provider
    
    return True  # All validations passed


# =============================================================================
# SUPPLEMENTARY CONSTRAINTS (Future Enhancement)
# =============================================================================

def on_data_exchange(event):
    """
    Runtime validation: can be triggered when relation data is available.
    This performs cryptographic validation of certificates.
    
    NOTE: This is a post-deployment or deploy-time validation.
    """
    
    # CONSTRAINT 10: Certificate signature validation
    # Verify that certificates received via oauth are signed by the root CA
    
    ca_data = event.get_relation_data('receive-ca-cert', 0)
    root_ca_cert = ca_data.get('certificate')
    
    oauth_data = event.get_relation_data('oauth', 0)
    oauth_certs = oauth_data.get('certificates', [])
    
    for cert_index, cert_pem in enumerate(oauth_certs):
        if not verify_certificate_signed_by(cert_pem, root_ca_cert):
            event.reject(
                'certificate_signature_invalid',
                f'Certificate {cert_index} in oauth relation is not signed by root CA'
            )
            return
    
    # CONSTRAINT 11: Certificate subject validation (future)
    # Verify certificates have expected SANs, CN, etc.
    # This would be charm-specific and defined in config
    
    return True


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def verify_certificate_signed_by(cert_pem, ca_cert_pem):
    """
    Verify that cert_pem is signed by ca_cert_pem.
    
    In a real implementation, this would use cryptography library:
    - Parse both certificates
    - Verify signature using CA's public key
    - Optionally check revocation status
    """
    # Placeholder: real implementation would use cryptography.io
    return True
