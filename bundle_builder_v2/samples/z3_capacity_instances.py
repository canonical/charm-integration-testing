"""
Capacity and multi-instance example.

Scenario:
- Two consumers (app_a, app_b) each require a postgresql_client provider.
- A Postgres charm has capacity 1 on its database endpoint.
- We allow creating multiple instances of Postgres (pg1, pg2) to satisfy demand.
- Z3 decides how many instances are needed and which consumer binds to which instance,
  minimizing total providers deployed.
"""

from z3 import Bool, Implies, Not, Optimize, Or, PbLe, sat


def capacity_example():
    solver = Optimize()

    # Consumers (fixed, required)
    app_a = Bool("app_a")
    app_b = Bool("app_b")
    solver.add(app_a, app_b)  # both must be present

    # Provider instances (optional)
    pg1 = Bool("postgresql_instance_1")
    pg2 = Bool("postgresql_instance_2")

    # Assignments: consumer -> provider instance
    a_pg1 = Bool("a_to_pg1")
    a_pg2 = Bool("a_to_pg2")
    b_pg1 = Bool("b_to_pg1")
    b_pg2 = Bool("b_to_pg2")

    # If assigned, provider must exist
    solver.add(Implies(a_pg1, pg1))
    solver.add(Implies(a_pg2, pg2))
    solver.add(Implies(b_pg1, pg1))
    solver.add(Implies(b_pg2, pg2))

    # Each consumer must be assigned to exactly one provider instance
    solver.add(Or(a_pg1, a_pg2))
    solver.add(Or(b_pg1, b_pg2))
    solver.add(Not(a_pg1) | Not(a_pg2))  # at most one
    solver.add(Not(b_pg1) | Not(b_pg2))

    # Capacity constraint: each Postgres instance can serve at most 1 consumer
    solver.add(PbLe([(a_pg1, 1), (b_pg1, 1)], 1))  # pg1 capacity 1
    solver.add(PbLe([(a_pg2, 1), (b_pg2, 1)], 1))  # pg2 capacity 1

    # Optimization: minimize number of provider instances
    solver.add_soft(Not(pg1), weight=10, id="min_providers")
    solver.add_soft(Not(pg2), weight=10, id="min_providers")

    print("Solving capacity example...")
    result = solver.check()

    if result != sat:
        print("No solution found")
        return

    model = solver.model()
    chosen_pg1 = model.evaluate(pg1, model_completion=True)
    chosen_pg2 = model.evaluate(pg2, model_completion=True)

    print("\nSelected provider instances:")
    if chosen_pg1:
        print("  - postgresql_instance_1")
    if chosen_pg2:
        print("  - postgresql_instance_2")

    print("\nAssignments:")
    if model.evaluate(a_pg1, model_completion=True):
        print("  app_a -> pg1")
    if model.evaluate(a_pg2, model_completion=True):
        print("  app_a -> pg2")
    if model.evaluate(b_pg1, model_completion=True):
        print("  app_b -> pg1")
    if model.evaluate(b_pg2, model_completion=True):
        print("  app_b -> pg2")

    print("\nExplanation:")
    print("- Capacity 1 forces two Postgres instances to satisfy two consumers")
    print("- Soft constraints minimize number of instances; with capacity=1, optimum uses pg1 and pg2")


if __name__ == "__main__":
    capacity_example()
