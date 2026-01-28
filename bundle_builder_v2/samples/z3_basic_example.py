"""
Basic Z3 example: Encoding charm integrations as SMT constraints.

This demonstrates the core concepts:
1. Boolean variables for apps and integrations
2. Hard constraints (interface matching, directionality)
3. Soft constraints (minimize apps/integrations)
4. Extracting the solution
"""

from z3 import And, Bool, Implies, Not, Optimize, sat


def basic_bundle_builder():
    """
    Goal: Build a minimal bundle that includes jimm and satisfies its dependencies.

    jimm needs:
    - database (postgresql_client) - required
    - oauth - required (via oauth or oauth-tls endpoint)
    - vault (vault-kv) - optional

    Let's see if Z3 can figure out the minimal set of apps and integrations.
    """

    # Use Optimize to handle soft constraints (minimization)
    solver = Optimize()

    # Step 1: Define variables for which apps are in the bundle
    # We'll say jimm is required (base graph), others are optional
    app_jimm = Bool("app_jimm")
    app_postgresql = Bool("app_postgresql")
    app_hydra = Bool("app_hydra")
    app_vault = Bool("app_vault")
    app_tls = Bool("app_tls_operator")

    # jimm is required (part of base graph)
    solver.add(app_jimm == True)

    # Step 2: Define integration variables
    # Integration: (app1, endpoint1, app2, endpoint2)
    # For simplicity, we'll name them by what they connect

    # jimm.database → postgresql.database
    integration_jimm_pg = Bool("integration_jimm_postgresql_database")

    # jimm.oauth → hydra.oauth
    integration_jimm_hydra = Bool("integration_jimm_hydra_oauth")

    # jimm.vault → vault.vault-kv
    integration_jimm_vault = Bool("integration_jimm_vault_kv")

    # hydra.pg-database → postgresql.database
    integration_hydra_pg = Bool("integration_hydra_postgresql_database")

    # postgresql.certificates → tls.certificates
    integration_pg_tls = Bool("integration_postgresql_tls_certs")

    # vault.tls-certificates-pki → tls.certificates
    integration_vault_tls = Bool("integration_vault_tls_certs")

    # Step 3: Hard constraints - structural rules

    # Rule: An integration can only exist if both apps exist
    solver.add(Implies(integration_jimm_pg, And(app_jimm, app_postgresql)))
    solver.add(Implies(integration_jimm_hydra, And(app_jimm, app_hydra)))
    solver.add(Implies(integration_jimm_vault, And(app_jimm, app_vault)))
    solver.add(Implies(integration_hydra_pg, And(app_hydra, app_postgresql)))
    solver.add(Implies(integration_pg_tls, And(app_postgresql, app_tls)))
    solver.add(Implies(integration_vault_tls, And(app_vault, app_tls)))

    # Rule: jimm's required endpoints must be integrated
    # jimm.database is required → must have integration_jimm_pg
    solver.add(Implies(app_jimm, integration_jimm_pg))

    # jimm must have oauth OR oauth-tls (we model both as oauth for simplicity here)
    # In this simple model, we only have oauth via hydra
    solver.add(Implies(app_jimm, integration_jimm_hydra))

    # Rule: If hydra exists, it needs its database
    solver.add(Implies(app_hydra, integration_hydra_pg))

    # Rule: Interface matching (already implicit in our variable naming)
    # In a real system, you'd assert:
    # Implies(integration_jimm_pg, interface_match("postgresql_client", "postgresql_client"))

    # Rule: Directionality - provides connects to requires
    # Already implicit in how we defined integrations
    # (jimm.database is requires, postgresql.database is provides)

    # Step 4: Soft constraints - minimize apps and integrations
    # We want the smallest bundle that satisfies requirements

    # Add soft constraint: prefer not to include optional apps
    # Weight 10 for each app (apps are more expensive than integrations)
    solver.add_soft(Not(app_postgresql), weight=10, id="minimize_apps")
    solver.add_soft(Not(app_hydra), weight=10, id="minimize_apps")
    solver.add_soft(Not(app_vault), weight=10, id="minimize_apps")
    solver.add_soft(Not(app_tls), weight=10, id="minimize_apps")

    # Weight 1 for each integration (prefer fewer integrations)
    solver.add_soft(Not(integration_jimm_pg), weight=1, id="minimize_integrations")
    solver.add_soft(Not(integration_jimm_hydra), weight=1, id="minimize_integrations")
    solver.add_soft(Not(integration_jimm_vault), weight=1, id="minimize_integrations")
    solver.add_soft(Not(integration_hydra_pg), weight=1, id="minimize_integrations")
    solver.add_soft(Not(integration_pg_tls), weight=1, id="minimize_integrations")
    solver.add_soft(Not(integration_vault_tls), weight=1, id="minimize_integrations")

    # Step 5: Solve
    print("Solving...")
    result = solver.check()

    if result == sat:
        model = solver.model()
        print("\n✓ Found valid bundle!\n")

        # Extract solution
        print("Applications:")
        apps = [
            ("jimm", app_jimm),
            ("postgresql", app_postgresql),
            ("hydra", app_hydra),
            ("vault", app_vault),
            ("tls-operator", app_tls),
        ]
        for name, var in apps:
            if model.evaluate(var, model_completion=True):
                print(f"  - {name}")

        print("\nIntegrations:")
        integrations = [
            ("jimm.database → postgresql.database", integration_jimm_pg),
            ("jimm.oauth → hydra.oauth", integration_jimm_hydra),
            ("jimm.vault → vault.vault-kv", integration_jimm_vault),
            ("hydra.pg-database → postgresql.database", integration_hydra_pg),
            ("postgresql.certificates → tls.certificates", integration_pg_tls),
            ("vault.tls-certificates-pki → tls.certificates", integration_vault_tls),
        ]
        for name, var in integrations:
            if model.evaluate(var, model_completion=True):
                print(f"  - {name}")

        print("\nExplanation:")
        print("Z3 found the minimal bundle:")
        print("- jimm requires database → adds postgresql")
        print("- jimm requires oauth → adds hydra")
        print("- hydra requires database → connects to postgresql (reuses it)")
        print("- vault and tls-operator not needed → excluded")

    else:
        print("✗ No valid bundle found (constraints are unsatisfiable)")


if __name__ == "__main__":
    print("=" * 60)
    print("Basic Z3 Bundle Builder Example")
    print("=" * 60)
    basic_bundle_builder()
