"""
Two-phase Z3 with alternative providers: pgbouncer + postgresql

Scenario:
- 3 consumers need postgresql_client interface
- Provider options:
  1. Direct: postgresql (capacity=1) → need 3 instances
  2. Pooled: pgbouncer (unlimited) → postgresql backend
     - Consumers connect to pgbouncer (unlimited capacity)
     - pgbouncer connects to postgresql backend

Phase 1: Compute max instances per provider type (conservative)
Phase 2: Choose optimal provider mix and minimize instances
"""

from z3 import And, Bool, If, Implies, Int, Not, Optimize, Or, PbLe, Sum, sat


def pgbouncer_example():
    """
    3 consumers (jimm, hydra, app_c) need postgresql_client.

    Providers:
    - postgresql: capacity=1 per instance
    - pgbouncer: unlimited capacity, requires postgresql backend
    """

    print("=" * 70)
    print("PHASE 1: Determine Upper Bounds")
    print("=" * 70)

    solver1 = Optimize()

    # Apps
    apps = ["jimm", "hydra", "app_c", "postgresql", "pgbouncer"]
    app_vars = {name: Bool(f"app_{name}") for name in apps}

    # Base apps required
    solver1.add(app_vars["jimm"] == True)
    solver1.add(app_vars["hydra"] == True)
    solver1.add(app_vars["app_c"] == True)

    # Counts
    postgresql_count = Int("postgresql_count")
    pgbouncer_count = Int("pgbouncer_count")
    solver1.add(postgresql_count >= 0)
    solver1.add(pgbouncer_count >= 0)

    # All 3 consumers need database
    total_db_consumers = 3

    # Worst case scenarios:
    # 1. All direct to postgresql: 3 postgresql (capacity=1 each)
    # 2. All via pgbouncer: 3 pgbouncer + 3 postgresql backends
    #    (each consumer gets own pgbouncer, each pgbouncer gets own postgresql)
    # Therefore: max postgresql = 3, max pgbouncer = 3

    solver1.add(postgresql_count <= total_db_consumers)
    solver1.add(pgbouncer_count <= total_db_consumers)

    # If using pgbouncer, it needs postgresql backend
    # (in worst case: 1 postgresql per pgbouncer)
    solver1.add(Implies(pgbouncer_count > 0, postgresql_count >= pgbouncer_count))

    # At least one provider must exist
    solver1.add(Or(postgresql_count > 0, pgbouncer_count > 0))

    # MAXIMIZE both counts (conservative upper bounds)
    solver1.maximize(postgresql_count + pgbouncer_count)

    print("\nSolving for maximum instances...")
    result1 = solver1.check()

    if result1 != sat:
        print("Phase 1 failed")
        return

    model1 = solver1.model()
    max_postgresql = model1.evaluate(postgresql_count).as_long()
    max_pgbouncer = model1.evaluate(pgbouncer_count).as_long()

    print("\n✓ Upper bounds computed:")
    print(f"  - Max postgresql instances: {max_postgresql}")
    print(f"  - Max pgbouncer instances: {max_pgbouncer}")
    print("  - Rationale: Worst case = 3 consumers × (1 pgbouncer + 1 postgresql each) = 6 instances")

    print("\n" + "=" * 70)
    print("PHASE 2: Minimize Bundle")
    print("=" * 70)

    solver2 = Optimize()

    # Apps
    app_vars2 = {name: Bool(f"app_{name}") for name in apps}
    solver2.add(app_vars2["jimm"] == True)
    solver2.add(app_vars2["hydra"] == True)
    solver2.add(app_vars2["app_c"] == True)

    # Provider instances (up to max from phase 1)
    postgresql_instances = [Bool(f"postgresql_{i}") for i in range(max_postgresql)]
    pgbouncer_instances = [Bool(f"pgbouncer_{i}") for i in range(max_pgbouncer)]

    # Integration variables
    # Consumers can connect to either postgresql or pgbouncer
    consumers = ["jimm", "hydra", "app_c"]

    # Consumer → postgresql direct integrations
    consumer_to_pg = {}
    for consumer in consumers:
        for i in range(max_postgresql):
            consumer_to_pg[(consumer, i)] = Bool(f"int_{consumer}_pg_{i}")

    # Consumer → pgbouncer integrations
    consumer_to_pgbouncer = {}
    for consumer in consumers:
        for i in range(max_pgbouncer):
            consumer_to_pgbouncer[(consumer, i)] = Bool(f"int_{consumer}_pgbouncer_{i}")

    # Pgbouncer → postgresql backend integrations
    pgbouncer_to_pg = {}
    for pb_i in range(max_pgbouncer):
        for pg_i in range(max_postgresql):
            pgbouncer_to_pg[(pb_i, pg_i)] = Bool(f"int_pgbouncer_{pb_i}_pg_{pg_i}")

    # Integration requires both endpoints exist
    for consumer in consumers:
        for i in range(max_postgresql):
            solver2.add(Implies(consumer_to_pg[(consumer, i)], And(app_vars2[consumer], postgresql_instances[i])))
        for i in range(max_pgbouncer):
            solver2.add(Implies(consumer_to_pgbouncer[(consumer, i)], And(app_vars2[consumer], pgbouncer_instances[i])))

    for pb_i in range(max_pgbouncer):
        for pg_i in range(max_postgresql):
            solver2.add(
                Implies(pgbouncer_to_pg[(pb_i, pg_i)], And(pgbouncer_instances[pb_i], postgresql_instances[pg_i]))
            )

    # Each consumer must connect to exactly one provider (postgresql or pgbouncer)
    for consumer in consumers:
        all_provider_connections = [consumer_to_pg[(consumer, i)] for i in range(max_postgresql)]
        all_provider_connections += [consumer_to_pgbouncer[(consumer, i)] for i in range(max_pgbouncer)]

        solver2.add(Or(all_provider_connections))  # at least one
        solver2.add(PbLe([(c, 1) for c in all_provider_connections], 1))  # at most one

    # Capacity constraint: each postgresql instance handles at most 1 consumer
    # (either direct consumer OR pgbouncer)
    for pg_i in range(max_postgresql):
        direct_consumers = Sum([If(consumer_to_pg[(consumer, pg_i)], 1, 0) for consumer in consumers])
        pgbouncer_consumers = Sum([If(pgbouncer_to_pg[(pb_i, pg_i)], 1, 0) for pb_i in range(max_pgbouncer)])
        total_load = direct_consumers + pgbouncer_consumers
        solver2.add(total_load <= 1)

    # If pgbouncer exists, it must connect to at least one postgresql
    for pb_i in range(max_pgbouncer):
        backend_connections = [pgbouncer_to_pg[(pb_i, pg_i)] for pg_i in range(max_postgresql)]
        solver2.add(Implies(pgbouncer_instances[pb_i], Or(backend_connections)))

    # MINIMIZE: total apps and instances
    for instance in postgresql_instances:
        solver2.add_soft(Not(instance), weight=10, id="min_instances")
    for instance in pgbouncer_instances:
        solver2.add_soft(Not(instance), weight=10, id="min_instances")

    print("\nSolving for minimal bundle...")
    print("  Options:")
    print("    A) 3 postgresql instances (direct connections)")
    print("    B) 1 pgbouncer + 1-3 postgresql (pooled)")

    result2 = solver2.check()

    if result2 != sat:
        print("Phase 2 failed")
        return

    model2 = solver2.model()

    print("\n✓ Found minimal bundle!\n")

    # Extract solution
    print("Applications:")
    active_pg = []
    active_pb = []

    for i, instance in enumerate(postgresql_instances):
        if model2.evaluate(instance, model_completion=True):
            active_pg.append(i)
            print(f"  - postgresql_{i}")

    for i, instance in enumerate(pgbouncer_instances):
        if model2.evaluate(instance, model_completion=True):
            active_pb.append(i)
            print(f"  - pgbouncer_{i}")

    for app in ["jimm", "hydra", "app_c"]:
        if model2.evaluate(app_vars2[app], model_completion=True):
            print(f"  - {app}")

    print("\nIntegrations:")
    for consumer in consumers:
        for pg_i in range(max_postgresql):
            if model2.evaluate(consumer_to_pg[(consumer, pg_i)], model_completion=True):
                print(f"  - {consumer} → postgresql_{pg_i}")
        for pb_i in range(max_pgbouncer):
            if model2.evaluate(consumer_to_pgbouncer[(consumer, pb_i)], model_completion=True):
                print(f"  - {consumer} → pgbouncer_{pb_i}")

    for pb_i in range(max_pgbouncer):
        for pg_i in range(max_postgresql):
            if model2.evaluate(pgbouncer_to_pg[(pb_i, pg_i)], model_completion=True):
                print(f"  - pgbouncer_{pb_i} → postgresql_{pg_i} (backend)")

    print("\nResult:")
    print(f"  - Phase 1: determined max {max_postgresql} postgresql, {max_pgbouncer} pgbouncer")
    print(f"  - Phase 2: chose {len(active_pg)} postgresql, {len(active_pb)} pgbouncer")
    if active_pb:
        print("  - Z3 selected pooled approach (fewer instances)")
    else:
        print("  - Z3 selected direct approach")


if __name__ == "__main__":
    print("Pgbouncer + PostgreSQL: Alternative Providers Example\n")
    pgbouncer_example()
    print("\n" + "=" * 70)
    print("Key insights:")
    print("=" * 70)
    print("""
1. Phase 1 computes conservative upper bounds per provider type
   - Max postgresql = 3 (worst case: 1 per consumer)
   - Max pgbouncer = 3 (worst case: 1 per consumer)
   - Each pgbouncer needs its own postgresql backend
   - Total worst case: 6 instances (3 pgbouncer + 3 postgresql)

2. Phase 2 explores provider combinations:
   - Option A: 3 postgresql (direct) → 3 instances total
   - Option B: 1 pgbouncer + 1 postgresql → 2 instances total
   - Option C: 3 pgbouncer + 3 postgresql → 6 instances total (worst)
   
3. Z3 minimizes total instances → chooses Option B (pooled approach)
   - All 3 consumers connect to 1 shared pgbouncer
   - Pgbouncer connects to 1 postgresql backend
   - Result: 2 instances instead of 3 or 6

This generalizes to any alternative provider topology!
""")
