Bundle Builder V3 Algorithm
=========================

This document explains the algorithmic foundation of **Bundle Builder V3**, the Proof of Concept (POC) implementation for constraint-based bundle generation.

The Core Problem
----------------

Generating a valid Juju bundle is a **Satisfiability Modulo Theories (SMT)** problem. We are not just looking for *any* combination of charms; we are looking for a combination that satisfies:

1.  **User Constraints**: "I want WordPress."
2.  **Structural Constraints**: "Every 'requires' endpoint must be connected to a 'provides' endpoint."
3.  **Charm Constraints**: "WordPress requires a database."
4.  **Mutual Exclusion**: "If I use MySQL 8, I cannot use the MariaDB interface."

The Algorithm: Iterative Domain Expansion
-----------------------------------------

SMT solvers (like Z3) operate on a fixed set of variables. However, the "universe" of all possible charms is too large to load into the solver at once.

To solve this, Bundle Builder V3 uses an **Iterative Domain Expansion** algorithm. It starts with a small universe (just the user's requested apps) and aggressively adds dependencies only when the solver proves they are missing.

.. mermaid::

    flowchart TD
        Start([User Request]) --> A[Initialize Domain]
        A --> B{Is Satisfiable?}
        B -- YES --> C[Optimize Solution]
        C --> D([Output Bundle])
        B -- NO --> E[Get Unsat Core]
        E --> F[Analyze Failure]
        F --> G{Can we expand?}
        G -- YES --> H[Add Missing Charms]
        H --> A
        G -- NO --> I([Error: Unresolvable])

Phase 1: Domain Initialization
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The tool creates a "Domain" (a symbol table) containing Z3 variables for:

*   **Charms**: Boolean variables (`exists_mysql`, `exists_wordpress`).
*   **Endpoints**: Integers and Booleans (`count_db_integrations`, `integrated_db`).
*   **Config**: Strings, Integers, or Booleans (`config_role`, `config_vip`).

Phase 2: The Solver Loop
~~~~~~~~~~~~~~~~~~~~~~~~

1.  **Constraint Injection**: The tool translates metadata into SMT assertions.
    *   *Metadata*: `requires: db` -> `assert(implies(exists_wordpress, integrated_db))`
2.  **Check SAT**: The solver attempts to find a valid assignment.
3.  **Handle UNSAT**: If the solver fails, it returns an **Unsatisfiable Core**—a list of specific assertions that conflicted.
    *   *Example Error*: "Constraint `wordpress_requires_db` failed."
    *   *Expansion*: The tool parses this error, queries Charmhub for charms that provide the `mysql` interface, and adds them to the domain for the next iteration.

Phase 3: Optimization
~~~~~~~~~~~~~~~~~~~~~

Once a valid solution (`SAT`) is found, it might be "bloated" (e.g., deploying 10 databases when 1 is enough). The tool runs a second pass with a **Soft Constraint** to minimize the total number of charms and integrations.

.. mermaid::

    graph LR
        subgraph Unoptimized
            WP1[WordPress] --> DB1[MySQL A]
            WP2[WordPress] --> DB2[MySQL B]
        end
        
        subgraph Optimized
            WP3[WordPress] --> DB3[MySQL Single]
            WP4[WordPress] --> DB3
        end
        
        style Optimized fill:#e1f5ff
        style Unoptimized fill:#ffebee

Domain Mapping (Implementation Details)
---------------------------------------

The solver does not reason about abstract Juju concepts directly. It maps them to mathematical primitives.

Application Constraints
~~~~~~~~~~~~~~~~~~~~~~~

Constraints like `limit: 1` are translated into integer arithmetic.

*   **Metadata**: `requires: db (limit: 1)`
*   **SMT Logic**:

.. code-block:: lisp

    (assert (<= count_db 1))

Mutual Exclusion
~~~~~~~~~~~~~~~~

Constraints like "A or B, but not both" are translated into boolean logic.

*   **Metadata**: `constraints: (assert (not (and db db-legacy)))`
*   **SMT Logic**:

.. code-block:: lisp

    (assert (not (and integrated_db integrated_db_legacy)))

Transitive Dependencies
~~~~~~~~~~~~~~~~~~~~~~~

One of the most powerful features is the ability to resolve long chains of dependencies automatically.

.. mermaid::

    graph TD
        User[User Request: "I want Grafana"]
        
        Grafana -->|Requires DB| SQLite[SQLite \n(Added by Solver)]
        Grafana -->|Requires Ingress| Traefik[Traefik \n(Added by Solver)]
        
        Traefik -->|Requires Certs| SSC[Self Signed Certs \n(Added by Solver)]
        
        style User fill:#d4edda
        style SQLite fill:#fff4e1
        style Traefik fill:#fff4e1
        style SSC fill:#fff4e1

The solver encounters:
1.  `exists_grafana` is TRUE (User Request).
2.  Constraint `grafana implies integrated_db` fails -> **UNSAT**.
3.  Tool adds `sqlite`.
4.  Constraint `grafana implies integrated_ingress` fails -> **UNSAT**.
5.  Tool adds `traefik`.
6.  Constraint `traefik implies integrated_certs` fails -> **UNSAT**.
7.  Tool adds `self_signed_certificates`.
8.  **SAT**.

Constraint Layering
-------------------

The constraints are applied in layers to ensure safety and correctness.

1.  **Structural Constraints (Base Layer)**: Physics of Juju. "You cannot integrate A to B if B does not exist." "An integration must have two ends."
2.  **Charm Constraints (Middle Layer)**: Rules defined by users in `metadata.yaml` or `constraints`.
3.  **User Constraints (Top Layer)**: "I explicitly want to deploy `mysql-router`."

.. mermaid::

    block-beta
        columns 1
        block:User
            userText["User Constraints\n(Input Bundle)"]
        end
        block:Charm
            charmText["Charm Constraints\n(metadata.yaml)"]
        end
        block:Structural
            structText["Structural Constraints\n(Juju Logic)"]
        end
        
        style User fill:#bbf,stroke:#333,stroke-width:2px
        style Charm fill:#dfd,stroke:#333,stroke-width:2px
        style Structural fill:#fdd,stroke:#333,stroke-width:2px

Conflict Resolution
-------------------

When the solver hits a "True Conflict" (e.g., User asks for A and B, but they are mutually exclusive), the **Unsatisfiable Core** allows the tool to report the exact cause.

*   **Scenario**: User requests `canonical-livepatch-server` and integrates BOTH `database` and `database-legacy`.
*   **Solver Output**: `unsat_core = [constraint_livepatch_mutex]`
*   **User Message**: "Error: Unable to generate bundle. The integration of ensure both `database` and `database-legacy` violates the constraint `livepatch_mutex`."
