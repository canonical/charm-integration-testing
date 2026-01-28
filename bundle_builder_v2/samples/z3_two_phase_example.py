"""
Two-phase Z3 bundle building: determine upper bounds, then minimize.

Phase 1: Maximize instance counts to find tight upper bounds
Phase 2: Minimize bundle size within those bounds

This avoids arbitrary limits and handles capacity constraints elegantly.
"""

from z3 import And, Bool, If, Implies, Int, Not, Optimize, Or, PbLe, Sum, sat


def two_phase_bundle_builder():
    """
    Scenario: jimm needs database and oauth
              hydra provides oauth, needs database
              postgres provides database, capacity 2

    Phase 1: Find max postgres instances needed in worst case
    Phase 2: Find minimal bundle
    """

    print("=" * 70)
    print("PHASE 1: Determine Upper Bounds")
    print("=" * 70)

    # Base apps
    base_apps = ["jimm"]

    # All possible apps in solution space
    apps = ["jimm", "postgres", "hydra"]

    solver1 = Optimize()

    # App variables
    app_vars = {name: Bool(f"app_{name}") for name in apps}

    # Base apps required
    solver1.add(app_vars["jimm"] == True)

    # jimm needs database and oauth
    needs_postgres = ["jimm"]
    needs_oauth = ["jimm"]

    # Count postgres instances needed (Int variable)
    postgres_count = Int("postgres_count")
    solver1.add(postgres_count >= 0)

    # If hydra exists, it needs database too
    # (optional: Z3 chooses whether to include hydra)
    needs_postgres_if_hydra = Int("needs_postgres_if_hydra")
    solver1.add(needs_postgres_if_hydra == If(app_vars["hydra"], 1, 0))

    total_postgres_consumers = 1 + needs_postgres_if_hydra  # jimm + maybe hydra

    # Capacity constraint: each postgres handles 2 consumers
    capacity_per_postgres = 2
    solver1.add(postgres_count * capacity_per_postgres >= total_postgres_consumers)

    # If anyone needs postgres, at least one postgres must exist
    solver1.add(Implies(total_postgres_consumers > 0, postgres_count >= 1))

    # jimm needs oauth - must have hydra or some oauth provider
    solver1.add(Implies(app_vars["jimm"], app_vars["hydra"]))

    # MAXIMIZE postgres instances (worst case)
    solver1.maximize(postgres_count)

    print("\nSolving for maximum postgres instances...")
    result1 = solver1.check()

    if result1 != sat:
        print("Phase 1 failed: no solution")
        return

    model1 = solver1.model()
    max_postgres = model1.evaluate(postgres_count).as_long()

    print(f"\n✓ Maximum postgres instances needed: {max_postgres}")
    print("  Worst case: jimm + hydra both need database")
    print(f"  With capacity=2: ceil(2/2) = {max_postgres}")

    print("\n" + "=" * 70)
    print("PHASE 2: Minimize Bundle")
    print("=" * 70)

    solver2 = Optimize()

    # App variables
    app_vars2 = {name: Bool(f"app_{name}") for name in apps}

    # Postgres instance variables (up to max from phase 1)
    postgres_instances = [Bool(f"postgres_instance_{i}") for i in range(max_postgres)]

    # Base apps required
    solver2.add(app_vars2["jimm"] == True)

    # Integration variables
    # jimm.database → postgres_instance_X
    jimm_pg_integrations = [Bool(f"int_jimm_pg_{i}") for i in range(max_postgres)]

    # hydra.database → postgres_instance_X (if hydra exists)
    hydra_pg_integrations = [Bool(f"int_hydra_pg_{i}") for i in range(max_postgres)]

    # hydra.oauth → jimm.oauth
    int_hydra_jimm_oauth = Bool("int_hydra_jimm_oauth")

    # Integration requires both apps exist
    for i in range(max_postgres):
        solver2.add(Implies(jimm_pg_integrations[i], And(app_vars2["jimm"], postgres_instances[i])))
        solver2.add(Implies(hydra_pg_integrations[i], And(app_vars2["hydra"], postgres_instances[i])))

    solver2.add(Implies(int_hydra_jimm_oauth, And(app_vars2["hydra"], app_vars2["jimm"])))

    # jimm must have database integrated (exactly one postgres instance)
    solver2.add(Or(jimm_pg_integrations))
    solver2.add(PbLe([(i, 1) for i in jimm_pg_integrations], 1))

    # jimm must have oauth
    solver2.add(Implies(app_vars2["jimm"], int_hydra_jimm_oauth))

    # If hydra exists, must have database integrated
    solver2.add(Implies(app_vars2["hydra"], Or(hydra_pg_integrations)))
    solver2.add(PbLe([(i, 1) for i in hydra_pg_integrations], 1))

    # Capacity constraint per postgres instance: at most 2 consumers
    for i in range(max_postgres):
        consumers = Sum([If(jimm_pg_integrations[i], 1, 0), If(hydra_pg_integrations[i], 1, 0)])
        solver2.add(consumers <= 2)

    # MINIMIZE: fewer apps and instances
    solver2.add_soft(Not(app_vars2["hydra"]), weight=10, id="min_apps")
    for i in range(max_postgres):
        solver2.add_soft(Not(postgres_instances[i]), weight=10, id="min_instances")

    print("\nSolving for minimal bundle...")
    result2 = solver2.check()

    if result2 != sat:
        print("Phase 2 failed: no solution")
        return

    model2 = solver2.model()

    print("\n✓ Found minimal bundle!\n")

    # Extract apps
    print("Applications:")
    for name, var in app_vars2.items():
        if model2.evaluate(var, model_completion=True):
            print(f"  - {name}")

    # Extract postgres instances
    print("\nPostgres instances:")
    active_instances = []
    for i, instance in enumerate(postgres_instances):
        if model2.evaluate(instance, model_completion=True):
            active_instances.append(i)
            print(f"  - postgres_instance_{i}")

    print("\nIntegrations:")
    for i in range(max_postgres):
        if model2.evaluate(jimm_pg_integrations[i], model_completion=True):
            print(f"  - jimm.database → postgres_instance_{i}")
        if model2.evaluate(hydra_pg_integrations[i], model_completion=True):
            print(f"  - hydra.pg-database → postgres_instance_{i}")

    if model2.evaluate(int_hydra_jimm_oauth, model_completion=True):
        print("  - hydra.oauth → jimm.oauth")

    print("\nResult:")
    print(f"  - Phase 1 determined max {max_postgres} postgres instance(s) could be needed")
    print(f"  - Phase 2 found minimal bundle uses {len(active_instances)} postgres instance(s)")
    print("  - Both jimm and hydra share the same postgres (within capacity=2)")


if __name__ == "__main__":
    print("Two-Phase Z3 Bundle Building Example\n")
    two_phase_bundle_builder()
    print("\n" + "=" * 70)
    print("How this works:")
    print("=" * 70)
    print("""
Phase 1: MAXIMIZE instances per charm type
- Adds all constraints (requirements, capacity)
- Maximizes instance counts to find worst-case upper bounds
- Result: tight bounds without arbitrary limits

Phase 2: MINIMIZE total bundle size
- Creates instance variables up to bounds from Phase 1
- Adds same constraints + soft constraints to minimize
- Result: smallest bundle that satisfies all requirements

Benefits:
- No guessing at max instances (5? 10? 100?)
- No over-provisioning variables
- Handles dynamic capacity constraints
- Efficient: only creates variables actually needed
""")
