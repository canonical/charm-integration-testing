"""
Advanced Z3 example: All constraint types for bundle building.

This demonstrates:
1. Mutually exclusive endpoints (XOR)
2. Conditional requirements (if-then)
3. Same application constraints
4. Channel matching
5. Bridge/transitivity rules
6. Complex optimization
"""

from z3 import And, Bool, Implies, Not, Optimize, Or, PbEq, sat


def advanced_bundle_builder():
    """
    Advanced example with all constraint types.

    Scenario: Build a bundle with jimm that:
    - Has exactly one of oauth or oauth-tls integrated (mutually exclusive)
    - If oauth-tls is used, must use TLS for everything (conditional)
    - Can optionally use vault for secrets
    - If vault is used, it can bridge certificates from tls-operator
    """

    solver = Optimize()

    # === Applications ===
    app_jimm = Bool("app_jimm")
    app_postgresql = Bool("app_postgresql")
    app_hydra = Bool("app_hydra")
    app_vault = Bool("app_vault")
    app_tls = Bool("app_tls_operator")

    solver.add(app_jimm == True)  # jimm is required

    # === Endpoints (track which endpoints are integrated) ===
    # For jimm
    ep_jimm_database = Bool("ep_jimm_database")
    ep_jimm_oauth = Bool("ep_jimm_oauth")
    ep_jimm_oauth_tls = Bool("ep_jimm_oauth_tls")
    ep_jimm_vault = Bool("ep_jimm_vault")

    # For postgresql
    ep_pg_database = Bool("ep_pg_database")
    ep_pg_certificates = Bool("ep_pg_certificates")

    # For hydra
    ep_hydra_oauth = Bool("ep_hydra_oauth")
    ep_hydra_database = Bool("ep_hydra_database")

    # For vault
    ep_vault_kv = Bool("ep_vault_kv")
    ep_vault_certificates_provides = Bool("ep_vault_certificates_provides")
    ep_vault_certificates_requires = Bool("ep_vault_certificates_requires")

    # For tls-operator
    ep_tls_certificates = Bool("ep_tls_certificates")

    # === Integrations ===
    # Format: (provider_app, provider_endpoint, consumer_app, consumer_endpoint)

    int_pg_jimm_db = Bool("int_postgresql_database__jimm_database")
    int_pg_hydra_db = Bool("int_postgresql_database__hydra_database")

    int_hydra_jimm_oauth = Bool("int_hydra_oauth__jimm_oauth")
    int_hydra_jimm_oauth_tls = Bool("int_hydra_oauth__jimm_oauth_tls")

    int_vault_jimm = Bool("int_vault_kv__jimm_vault")

    int_tls_pg = Bool("int_tls_certificates__postgresql_certificates")
    int_tls_vault = Bool("int_tls_certificates__vault_certificates")

    # === CONSTRAINT TYPE 1: Integration requires both apps ===
    solver.add(Implies(int_pg_jimm_db, And(app_postgresql, app_jimm)))
    solver.add(Implies(int_pg_hydra_db, And(app_postgresql, app_hydra)))
    solver.add(Implies(int_hydra_jimm_oauth, And(app_hydra, app_jimm)))
    solver.add(Implies(int_hydra_jimm_oauth_tls, And(app_hydra, app_jimm)))
    solver.add(Implies(int_vault_jimm, And(app_vault, app_jimm)))
    solver.add(Implies(int_tls_pg, And(app_tls, app_postgresql)))
    solver.add(Implies(int_tls_vault, And(app_tls, app_vault)))

    # === CONSTRAINT TYPE 2: Endpoint integrated ↔ at least one integration uses it ===
    solver.add(ep_jimm_database == int_pg_jimm_db)
    solver.add(ep_jimm_oauth == int_hydra_jimm_oauth)
    solver.add(ep_jimm_oauth_tls == int_hydra_jimm_oauth_tls)
    solver.add(ep_jimm_vault == int_vault_jimm)

    solver.add(ep_pg_database == Or(int_pg_jimm_db, int_pg_hydra_db))
    solver.add(ep_pg_certificates == int_tls_pg)

    solver.add(ep_hydra_oauth == Or(int_hydra_jimm_oauth, int_hydra_jimm_oauth_tls))
    solver.add(ep_hydra_database == int_pg_hydra_db)

    solver.add(ep_vault_kv == int_vault_jimm)
    solver.add(ep_vault_certificates_requires == int_tls_vault)
    # vault.certificates (provides) is integrated if it provides certs to anyone (not modeled here for simplicity)

    solver.add(ep_tls_certificates == Or(int_tls_pg, int_tls_vault))

    # === CONSTRAINT TYPE 3: Required endpoints ===
    # jimm.database is required
    solver.add(Implies(app_jimm, ep_jimm_database))

    # === CONSTRAINT TYPE 4: Mutually exclusive endpoints (exactly one) ===
    # jimm must have exactly one of oauth or oauth-tls
    solver.add(PbEq([(ep_jimm_oauth, 1), (ep_jimm_oauth_tls, 1)], 1))

    # === CONSTRAINT TYPE 5: Conditional requirements ===
    # If oauth-tls is used, postgresql must use TLS certificates
    solver.add(Implies(ep_jimm_oauth_tls, ep_pg_certificates))

    # If hydra exists, it must have database integrated
    solver.add(Implies(app_hydra, ep_hydra_database))

    # === CONSTRAINT TYPE 6: Same application constraint ===
    # Example: if we had a rule "ep1 and ep2 must integrate with same app"
    # For now, this is implicit in our integration definitions
    # Real encoding: If int_A_ep1_X and int_A_ep2_Y, then X == Y
    # Would need auxiliary variables to track "which app" each endpoint connects to

    # === CONSTRAINT TYPE 7: Channel matching ===
    # Simplified: assume all apps use compatible channels
    # Real encoding: Would add String variables for channels and equality constraints

    # === CONSTRAINT TYPE 8: Bridge/transitivity ===
    # If postgresql gets certificates from tls-operator, it "has certificates"
    # If vault gets certificates from tls-operator via its requires endpoint,
    # then vault can provide certificates (bridging)

    # Create auxiliary variable: vault_has_certificates
    vault_has_certificates = Bool("vault_has_certificates")
    solver.add(vault_has_certificates == ep_vault_certificates_requires)

    # If vault is used to provide certificates to something, it must have certificates
    # (In this example, vault doesn't provide to anyone, but the pattern would be:)
    # solver.add(Implies(ep_vault_certificates_provides, vault_has_certificates))

    # === OPTIMIZATION: Minimize apps and integrations ===

    # Prefer not to include optional apps (weight 10)
    solver.add_soft(Not(app_postgresql), weight=10, id="min_apps")
    solver.add_soft(Not(app_hydra), weight=10, id="min_apps")
    solver.add_soft(Not(app_vault), weight=10, id="min_apps")
    solver.add_soft(Not(app_tls), weight=10, id="min_apps")

    # Prefer fewer integrations (weight 1)
    for integration in [
        int_pg_jimm_db,
        int_pg_hydra_db,
        int_hydra_jimm_oauth,
        int_hydra_jimm_oauth_tls,
        int_vault_jimm,
        int_tls_pg,
        int_tls_vault,
    ]:
        solver.add_soft(Not(integration), weight=1, id="min_integrations")

    # Prefer oauth over oauth-tls (simpler setup)
    solver.add_soft(Not(ep_jimm_oauth_tls), weight=5, id="prefer_simple")

    # === SOLVE ===
    print("Solving with advanced constraints...")
    print("Rules:")
    print("  - jimm needs database (required)")
    print("  - jimm needs exactly one of: oauth OR oauth-tls")
    print("  - if oauth-tls used → postgresql must use TLS")
    print("  - if hydra used → must connect its database")
    print("  - prefer minimal bundle")
    print()

    result = solver.check()

    if result == sat:
        model = solver.model()
        print("✓ Found valid bundle!\n")

        # Extract applications
        print("Applications:")
        for name, var in [
            ("jimm", app_jimm),
            ("postgresql", app_postgresql),
            ("hydra", app_hydra),
            ("vault", app_vault),
            ("tls-operator", app_tls),
        ]:
            if model.evaluate(var, model_completion=True):
                print(f"  - {name}")

        # Extract integrations
        print("\nIntegrations:")
        integration_names = [
            ("postgresql:database ← jimm:database", int_pg_jimm_db),
            ("postgresql:database ← hydra:pg-database", int_pg_hydra_db),
            ("hydra:oauth → jimm:oauth", int_hydra_jimm_oauth),
            ("hydra:oauth → jimm:oauth-tls", int_hydra_jimm_oauth_tls),
            ("vault:vault-kv → jimm:vault", int_vault_jimm),
            ("tls-operator:certificates → postgresql:certificates", int_tls_pg),
            ("tls-operator:certificates → vault:tls-certificates-pki", int_tls_vault),
        ]
        for name, var in integration_names:
            if model.evaluate(var, model_completion=True):
                print(f"  - {name}")

        print("\nEndpoints integrated:")
        ep_names = [
            ("jimm.oauth", ep_jimm_oauth),
            ("jimm.oauth-tls", ep_jimm_oauth_tls),
            ("jimm.database", ep_jimm_database),
            ("jimm.vault", ep_jimm_vault),
        ]
        for name, var in ep_names:
            if model.evaluate(var, model_completion=True):
                print(f"  - {name}")

        print("\nSolver chose:")
        if model.evaluate(ep_jimm_oauth, model_completion=True):
            print("  - Plain oauth (simpler, no TLS needed)")
        else:
            print("  - oauth-tls (forces TLS certificates for postgresql)")

    else:
        print("✗ No valid bundle found")


if __name__ == "__main__":
    print("=" * 70)
    print("Advanced Z3 Bundle Builder - All Constraint Types")
    print("=" * 70)
    print()
    advanced_bundle_builder()
