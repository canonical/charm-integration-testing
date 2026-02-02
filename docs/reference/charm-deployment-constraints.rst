Charm Deployment Constraints
=============================

This document describes the various constraint types observed in real-world charm deployments, independent of any specific implementation.

These constraints define how charms can integrate with each other, what requirements they have, and what limitations exist on their endpoints.

1. Optional vs Required Integrations
-------------------------------------

**Definition**: Whether an integration endpoint must be satisfied for a valid bundle.

.. note::

   Stock charm metadata YAML supports expressing optional/required status directly.

1.1 Always Required
~~~~~~~~~~~~~~~~~~~

**Behavior**: The endpoint must have at least one integration for the bundle to be valid.

**Example**: WordPress requires MySQL database, Hydra requires PostgreSQL database

.. mermaid::

   graph LR
       wordpress[wordpress]
       mysql[mysql]
       
       wordpress -->|database<br/>REQUIRED| mysql
       
       style wordpress fill:#e1f5ff
       style mysql fill:#fff4e1

1.2 Always Optional
~~~~~~~~~~~~~~~~~~~

**Behavior**: The endpoint may or may not have integrations. Bundle is valid either way.

**Example**: Hydra can optionally integrate with Prometheus for logging

.. mermaid::

   graph LR
       hydra[hydra]
       prometheus[prometheus]
       
       hydra -.->|logging<br/>OPTIONAL| prometheus
       
       style hydra fill:#e1f5ff
       style prometheus fill:#fff4e1
       
       classDef optional stroke-dasharray: 5 5
       class hydra optional

2. Integration Limits
----------------------

**Definition**: Constraints on how many integrations an endpoint can have simultaneously.

.. note::

   Stock charm metadata YAML supports expressing integration limits directly.

2.1 Single Integration (limit: 1)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Behavior**: Endpoint can have at most one active integration.

**Example**: ``content-cache-k8s:nginx-proxy`` has limit: 1

.. mermaid::

   graph LR
       content-cache[content-cache-k8s<br/>nginx-proxy<br/>limit: 1]
       
       wordpress[wordpress]
       mattermost[mattermost]
       
       wordpress -->|✓| content-cache
       mattermost -.->|✗ limit reached| content-cache
       
       style content-cache fill:#fff4e1
       style wordpress fill:#e1f5ff
       style mattermost fill:#ffebee

2.2 Unlimited Integrations
~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Behavior**: Endpoint can integrate with any number of consumers.

**Example**: Self-signed certificates can provide certificates to multiple consumers (Traefik, Grafana, etc.)

.. mermaid::

   graph LR
       traefik[traefik]
       grafana[grafana]
       wordpress[wordpress]
       certs[self-signed-certificates<br/>certificates]
       
       traefik --> certs
       grafana --> certs
       wordpress --> certs
       
       style certs fill:#fff4e1
       style traefik fill:#e1f5ff
       style grafana fill:#e1f5ff
       style wordpress fill:#e1f5ff

3. Mutual Exclusion
-------------------

**Definition**: Two or more endpoints cannot both be integrated simultaneously. Only one can be active at a time.

**Example**: Canonical Livepatch Server has ``database`` and ``database-legacy`` endpoints - only one can be used

.. mermaid::

   graph LR
       subgraph livepatch[canonical-livepatch-server-k8s]
           database[database]
           database-legacy[database-legacy]
       end
       
       postgresql[postgresql]
       
       database -->|✓ Option A| postgresql
       database-legacy -.->|✗ mutex| postgresql
       
       style database fill:#e1f5ff
       style database-legacy fill:#ffebee
       style postgresql fill:#fff4e1

**Alternative configuration**:

.. mermaid::

   graph LR
       subgraph livepatch[canonical-livepatch-server-k8s]
           database[database]
           database-legacy[database-legacy]
       end
       
       mysql[mysql]
       
       database -.->|✗ mutex| mysql
       database-legacy -->|✓ Option B| mysql
       
       style database fill:#ffebee
       style database-legacy fill:#e1f5ff
       style mysql fill:#fff4e1

**Behavior**: The charm can use either ``database`` or ``database-legacy``, but not both. This allows supporting multiple versions or migration paths while ensuring only one is active.

4. Conditionally Required Endpoints
------------------------------------

**Definition**: An endpoint that is optional by default, but becomes required when certain conditions are met (typically when another endpoint is integrated).

**Example**: Vault's ``tls-certificates-pki`` endpoint becomes required when ``vault-pki`` is integrated

.. mermaid::

   graph TB
       subgraph Scenario A: Vault without PKI
           vault1[vault-k8s]
           app1[some-app]
           
           vault1 -->|vault-kv| app1
           vault1 -.->|tls-certificates-pki<br/>OPTIONAL| none1[ ]
           
           style vault1 fill:#e1f5ff
           style app1 fill:#fff4e1
           style none1 fill:#ffffff,stroke:#ffffff
       end
       
       subgraph Scenario B: Vault with PKI
           vault2[vault-k8s]
           consumer[certificate-consumer]
           parent-ca[self-signed-certificates]
           
           vault2 -->|vault-pki| consumer
           vault2 -->|tls-certificates-pki<br/>REQUIRED| parent-ca
           
           style vault2 fill:#e1f5ff
           style consumer fill:#fff4e1
           style parent-ca fill:#fff4e1
       end

**Behavior**: 

- When ``vault-pki`` is **not** integrated: ``tls-certificates-pki`` is optional
- When ``vault-pki`` **is** integrated: ``tls-certificates-pki`` becomes required

This ensures that when Vault provides certificates as an intermediate CA, it must have a parent CA integration.

5. Capability Requirements
---------------------------

**Definition**: An endpoint must integrate with a specific charm or set of charms that provide particular capabilities, even when multiple charms provide the same interface.

**Example**: Temporal-k8s has two endpoints that both use the ``temporal`` interface:

- The ``admin`` endpoint requires temporal-admin-k8s specifically
- The ``ui`` endpoint requires temporal-ui-k8s specifically

Both providers offer the same ``temporal`` interface, but temporal-k8s needs to distinguish between them.

.. mermaid::

   graph LR
       temporal[temporal-k8s<br/>requires: admin temporal<br/>requires: ui temporal]
       temporal-admin[temporal-admin-k8s<br/>provides: temporal]
       temporal-ui[temporal-ui-k8s<br/>provides: temporal]
       
       temporal -->|admin endpoint<br/>must be temporal-admin-k8s| temporal-admin
       temporal -->|ui endpoint<br/>must be temporal-ui-k8s| temporal-ui
       
       style temporal fill:#e1f5ff
       style temporal-admin fill:#fff4e1
       style temporal-ui fill:#fff4e1

**Invalid Configuration**:

.. mermaid::

   graph LR
       temporal[temporal-k8s<br/>requires: admin temporal]
       temporal-ui[temporal-ui-k8s<br/>provides: temporal]
       
       temporal -.->|admin endpoint<br/>✗ wrong provider<br/>same interface| temporal-ui
       
       style temporal fill:#e1f5ff
       style temporal-ui fill:#ffebee

**Behavior**: 

- Both temporal-admin-k8s and temporal-ui-k8s provide the same ``temporal`` interface
- The ``admin`` endpoint of temporal-k8s must integrate specifically with temporal-admin-k8s
- The ``ui`` endpoint of temporal-k8s must integrate specifically with temporal-ui-k8s
- Interface matching alone is insufficient - the charm must be able to specify which provider is acceptable

This constraint type allows a charm to distinguish between different providers of the same interface based on the specific capabilities or functionality each provider offers.

6. Acyclic Constraints
-----------------------

**Definition**: The integration graph must not contain cycles - following the directed edges from requires to provides endpoints should never return to a previously visited application.

**Invalid Example**: pgbouncer-k8s instances forming a cycle

.. mermaid::

   graph LR
       pgb1[pgbouncer-k8s instance 1<br/>requires: backend-database<br/>provides: database]
       pgb2[pgbouncer-k8s instance 2<br/>requires: backend-database<br/>provides: database]
       
       pgb1 -->|requires backend-database| pgb2
       pgb2 -->|requires backend-database| pgb1
       
       style pgb1 fill:#ffebee
       style pgb2 fill:#ffebee

**Valid Example**: grafana-agent-k8s with bidirectional relationships to postgresql-k8s

.. mermaid::

   graph LR
       ga[grafana-agent-k8s<br/>requires: grafana-dashboards<br/>provides: logging]
       pg[postgresql-k8s<br/>provides: grafana-dashboards<br/>requires: logging]
       
       ga -->|requires grafana-dashboards| pg
       pg -->|requires logging| ga
       
       style ga fill:#d4edda
       style pg fill:#d4edda

**Behavior**:

- **pgbouncer-k8s cycle is invalid**: If pgbouncer-1 provides database to pgbouncer-2, and pgbouncer-2 provides database to pgbouncer-1, the integration graph contains a cycle
- **grafana-agent-k8s bidirectional is valid**: grafana-agent requires grafana-dashboards FROM postgresql-k8s, and postgresql-k8s requires logging FROM grafana-agent. This creates two separate directed edges but no cycle:

  - Edge 1: grafana-agent → postgresql (for dashboards)
  - Edge 2: postgresql → grafana-agent (for logging)
  - Following edges: grafana-agent → postgresql → grafana-agent would require following both "requires dashboards" and "requires logging" from the same application, which doesn't happen

7. Same Application Constraints
--------------------------------

**Definition**: When a charm integrates multiple endpoints with providers, certain endpoints must integrate with the same application instance.

**Example**: mongodb-k8s has two endpoints for LDAP functionality:

- ``ldap`` - connects to an LDAP provider for authentication
- ``ldap-certificate-transfer`` - receives certificates for secure LDAP connections

When mongodb-k8s integrates its ``ldap`` endpoint with a provider (like glauth-k8s), it must also integrate its ``ldap-certificate-transfer`` endpoint with **the same glauth-k8s instance**.

.. mermaid::

   graph LR
       mongo[mongodb-k8s<br/>requires: ldap<br/>requires: ldap-certificate-transfer]
       glauth[glauth-k8s<br/>provides: ldap<br/>provides: send-ca-cert]
       
       mongo -->|ldap| glauth
       mongo -->|ldap-certificate-transfer| glauth
       
       style mongo fill:#e1f5ff
       style glauth fill:#d4edda

**Invalid Configuration**:

.. mermaid::

   graph LR
       mongo[mongodb-k8s<br/>requires: ldap<br/>requires: ldap-certificate-transfer]
       glauth1[glauth-k8s instance 1<br/>provides: ldap]
       glauth2[glauth-k8s instance 2<br/>provides: send-ca-cert]
       
       mongo -->|ldap| glauth1
       mongo -.->|ldap-certificate-transfer<br/>✗ different instance| glauth2
       
       style mongo fill:#e1f5ff
       style glauth1 fill:#ffebee
       style glauth2 fill:#ffebee

**Behavior**:

- The ``ldap`` and ``ldap-certificate-transfer`` endpoints of mongodb-k8s must integrate with the same application instance
- If mongodb integrates ``ldap`` with glauth-k8s-1, then ``ldap-certificate-transfer`` must also integrate with glauth-k8s-1 (not glauth-k8s-2 or any other provider)
- This ensures that the certificates received match the LDAP service being used
- The constraint is mutual: if either endpoint is integrated, the other must be integrated with the same provider

This constraint type is common when one service provides both a primary capability and supporting resources (like certificates) that must come from the same source.

8. Version Compatibility Constraints
-------------------------------------

**Definition**: When multiple instances of a charm integrate with each other (forming a cluster or replication set), they must all be deployed from compatible charm versions.

**Version Components**:

- **Track**: Major version line (e.g., ``14``, ``1.0``)
- **Risk**: Stability level (``stable``, ``candidate``, ``beta``, ``edge``)
- **Revision**: Specific build number

**Example**: postgresql-k8s replication requires all database instances in the replication set to be from the same channel (track + risk combination).

**Valid Configuration - Same Channel**:

.. mermaid::

   graph LR
       pg1[postgresql-k8s-1<br/>14/stable]
       pg2[postgresql-k8s-2<br/>14/stable]
       
       pg1 <-->|replication| pg2
       
       style pg1 fill:#d4edda
       style pg2 fill:#d4edda

**Invalid Configuration - Different Channels**:

.. mermaid::

   graph LR
       pg1[postgresql-k8s-1<br/>14/stable]
       pg2[postgresql-k8s-2<br/>14/edge]
       
       pg1 -.->|replication<br/>✗ risk mismatch| pg2
       
       style pg1 fill:#ffebee
       style pg2 fill:#ffebee

**Behavior**:

- All postgresql instances participating in replication must be deployed from compatible versions
- Mixing different risks (e.g., stable with edge) in a replication cluster can cause incompatibilities
- Different tracks (e.g., 12/stable vs 14/stable) are typically incompatible for replication
- Version constraints ensure protocol compatibility and prevent split-brain scenarios
- The specific constraint level (track, risk, or revision) depends on the charm's compatibility guarantees

This constraint type ensures that all instances in a cluster run compatible versions, preventing issues from version mismatches during replication or clustering operations.

9. Minimum Observability Constraints
-------------------------------------

**Definition**: A charm must provide at least N endpoints from a set of M possible observability or monitoring endpoints.

**Example**: grafana-agent-k8s requires at least one of: ``metrics-endpoint``, ``logging-provider``, ``tracing-provider``, or ``grafana-dashboards-consumer`` to be integrated.

.. mermaid::

   graph LR
       ga[grafana-agent-k8s<br/>At least 1 of:<br/>- metrics<br/>- logging<br/>- tracing<br/>- dashboards]
       prom[prometheus<br/>provides: metrics]
       
       ga -->|metrics ✓| prom
       
       style ga fill:#d4edda
       style prom fill:#fff4e1

**Invalid Configuration - None Integrated**:

.. mermaid::

   graph LR
       ga[grafana-agent-k8s<br/>At least 1 of:<br/>- metrics<br/>- logging<br/>- tracing<br/>- dashboards]
       
       style ga fill:#ffebee

**Behavior**:

- grafana-agent-k8s must have at least one observability integration to be useful
- All four endpoints are individually optional
- But at least one must be satisfied for a valid bundle
- This ensures the agent has data to collect and forward

This constraint type is useful for charms that aggregate or relay data from multiple optional sources, where having zero sources makes the charm non-functional.

10. Minimum Cardinality Constraints
------------------------------------

**Definition**: An endpoint requires a minimum number of integrations to function properly.

**Example**: A load balancer might require at least 2 backend integrations to provide high availability.

**Behavior**:

- An endpoint must have at least N integrations (where N > 0)
- Different from "required" which only ensures N ≥ 1
- Useful for clustering, redundancy, or quorum requirements

**Note**: This constraint type has not been observed in current charm deployments but may be needed for future high-availability patterns.

11. Transitive Capabilities
---------------------------

**Definition**: A capability required by a charm can be satisfied transitively through a chain of integrations, where intermediate charms bridge the capability from the original provider to the final consumer.

**Example**: juju-jimm-k8s requires certificates from self-signed-certificates. The certificates flow through this integration chain:

1. self-signed-certificates provides certificates to traefik-k8s
2. traefik-k8s bridges certificates to hydra through ingress integration
3. hydra bridges certificates to juju-jimm-k8s through oauth integration

.. mermaid::

   graph LR
       jimm[juju-jimm-k8s<br/>requires: oauth<br/>requires: receive-ca-cert]
       hydra[hydra<br/>provides: oauth<br/>requires: public-ingress]
       traefik[traefik-k8s<br/>provides: ingress<br/>requires: certificates]
       ssc[self-signed-certificates<br/>provides: certificates]
       
       ssc -->|certificates| traefik
       traefik -->|ingress| hydra
       hydra -->|oauth| jimm
       ssc -.->|receive-ca-cert<br/>REQUIRED| jimm
       
       style jimm fill:#e1f5ff
       style hydra fill:#fff4e1
       style traefik fill:#fff4e1
       style ssc fill:#d4edda

**Behavior**:

- juju-jimm-k8s integrates with hydra for oauth
- hydra integrates with traefik-k8s for ingress
- traefik-k8s integrates with self-signed-certificates for certificates
- The certificates flow through the chain: self-signed-certificates → traefik → hydra → juju-jimm
- **Additionally, juju-jimm-k8s must have a direct integration with self-signed-certificates** (receive-ca-cert endpoint)
- This direct integration is required so juju-jimm can receive the CA certificate to verify the certificate chain it receives through the oauth integration

This constraint type shows that capabilities can propagate through integration chains, but charms may still need direct access to the root capability provider to verify or validate the transitive capabilities they receive.
