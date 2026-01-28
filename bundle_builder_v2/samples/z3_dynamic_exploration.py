"""
Dynamic graph exploration with Z3.

This demonstrates how to:
1. Start with base requirements
2. Query for potential providers (simulating Charmhub)
3. Build solution space with all possibilities
4. Let Z3 choose the optimal subset
"""

from dataclasses import dataclass
from typing import List

from z3 import Bool, Implies, Not, Optimize, Or, sat


@dataclass
class CharmMetadata:
    """Simplified charm metadata."""

    name: str
    provides: list[str]  # list of interface names
    requires: list[str]  # list of interface names
    priority: int = 1  # lower = prefer this charm


# Simulated Charmhub database
CHARMHUB_DB = {
    "jimm": CharmMetadata("jimm", provides=[], requires=["postgresql_client", "oauth"]),
    "postgresql": CharmMetadata("postgresql-k8s", provides=["postgresql_client"], requires=[]),
    "mysql": CharmMetadata("mysql-k8s", provides=["postgresql_client"], requires=[], priority=2),
    "hydra": CharmMetadata("hydra", provides=["oauth"], requires=["postgresql_client"]),
    "keycloak": CharmMetadata("keycloak", provides=["oauth"], requires=["postgresql_client"], priority=2),
    "dex": CharmMetadata("dex", provides=["oauth"], requires=[], priority=3),
}


def query_charmhub(interface: str, role: str) -> List[CharmMetadata]:
    """
    Simulate querying Charmhub for charms that provide/require an interface.

    Args:
        interface: The interface name (e.g., "oauth", "postgresql_client")
        role: "provides" or "requires"

    Returns:
        List of charms that match
    """
    results = []
    for charm in CHARMHUB_DB.values():
        if role == "provides" and interface in charm.provides:
            results.append(charm)
        elif role == "requires" and interface in charm.requires:
            results.append(charm)
    return results


def build_solution_space(base_apps: List[str], max_depth: int = 3):
    """
    Explore the graph of possible charms, building complete solution space.

    Args:
        base_apps: List of charm names that MUST be included
        max_depth: How deep to explore transitive dependencies

    Returns:
        Dictionary mapping charm names to CharmMetadata for all discovered charms
    """
    solution_space = {}
    to_explore = [(name, 0) for name in base_apps]  # (charm_name, depth)
    explored = set()

    while to_explore:
        charm_name, depth = to_explore.pop(0)

        if charm_name in explored or depth > max_depth:
            continue

        explored.add(charm_name)

        if charm_name not in CHARMHUB_DB:
            print(f"Warning: {charm_name} not in Charmhub")
            continue

        charm = CHARMHUB_DB[charm_name]
        solution_space[charm_name] = charm

        # For each requirement, find all possible providers
        for required_interface in charm.requires:
            providers = query_charmhub(required_interface, "provides")
            print(
                f"  {charm_name} needs {required_interface} → found {len(providers)} providers: {[p.name for p in providers]}"
            )

            for provider in providers:
                if provider.name not in explored:
                    to_explore.append((provider.name, depth + 1))

    return solution_space


def build_bundle_with_z3(base_apps: List[str]):
    """
    Build a minimal bundle starting from base_apps.

    1. Explore graph to find all possible charms
    2. Create Z3 variables for each
    3. Add constraints
    4. Solve for minimal valid bundle
    """

    print("=" * 70)
    print("Phase 1: Exploring solution space")
    print("=" * 70)
    print(f"Base apps: {base_apps}\n")

    solution_space = build_solution_space(base_apps)

    print(f"\nDiscovered {len(solution_space)} total charms in solution space:")
    for name in solution_space.keys():
        print(f"  - {name}")

    print("\n" + "=" * 70)
    print("Phase 2: Z3 constraint solving")
    print("=" * 70)

    solver = Optimize()

    # Create Boolean variable for each discovered charm
    app_vars = {name: Bool(f"app_{name}") for name in solution_space.keys()}

    # Base apps are required
    for app_name in base_apps:
        if app_name in app_vars:
            solver.add(app_vars[app_name] == True)

    # For each app, if it's included, its requirements must be satisfied
    for app_name, app_metadata in solution_space.items():
        app_var = app_vars[app_name]

        for required_interface in app_metadata.requires:
            # Find all charms that can provide this interface
            providers = query_charmhub(required_interface, "provides")
            provider_vars = [app_vars[p.name] for p in providers if p.name in app_vars]

            if provider_vars:
                # If this app is included, at least one provider must be included
                solver.add(Implies(app_var, Or(provider_vars)))
                print(
                    f"Constraint: {app_name} needs {required_interface} → must include one of {[p.name for p in providers]}"
                )
            else:
                print(f"Warning: No provider found for {app_name}.{required_interface}")

    # Optimization: minimize number of apps, weighted by priority
    print("\nOptimization: Prefer fewer apps, respect priorities")
    for app_name, app_metadata in solution_space.items():
        if app_name not in base_apps:  # Don't penalize base apps
            weight = 10 * app_metadata.priority  # Higher priority = higher weight to exclude
            solver.add_soft(Not(app_vars[app_name]), weight=weight, id="minimize")
            if app_metadata.priority > 1:
                print(f"  - {app_name} has priority {app_metadata.priority} (less preferred)")

    print("\nSolving...")
    result = solver.check()

    if result == sat:
        model = solver.model()
        print("\n✓ Found optimal bundle!\n")

        print("Selected applications:")
        selected = []
        for app_name, app_var in app_vars.items():
            if model.evaluate(app_var, model_completion=True):
                selected.append(app_name)
                metadata = solution_space[app_name]
                marker = "★" if app_name in base_apps else " "
                print(f"  {marker} {app_name}")
                if metadata.requires:
                    print(f"      requires: {', '.join(metadata.requires)}")
                if metadata.provides:
                    print(f"      provides: {', '.join(metadata.provides)}")

        print(f"\nTotal apps: {len(selected)}")

        # Show what alternatives were available but not chosen
        print("\nAlternatives considered but not selected:")
        for app_name, app_var in app_vars.items():
            if not model.evaluate(app_var, model_completion=True):
                metadata = solution_space[app_name]
                print(f"  ✗ {app_name} (priority {metadata.priority})")

        return selected

    else:
        print("✗ No valid bundle found (constraints unsatisfiable)")
        return None


if __name__ == "__main__":
    print("Dynamic Bundle Building with Z3\n")
    print("Scenario: Start with jimm, let Z3 discover and choose providers\n")

    bundle = build_bundle_with_z3(base_apps=["jimm"])

    print("\n" + "=" * 70)
    print("How this works:")
    print("=" * 70)
    print("""
1. Started with base requirement: jimm
2. Discovered jimm needs: postgresql_client, oauth
3. Queried Charmhub and found:
   - postgresql_client providers: postgresql, mysql
   - oauth providers: hydra, keycloak, dex
4. Discovered transitive dependencies:
   - hydra needs postgresql_client (can reuse!)
   - keycloak needs postgresql_client (can reuse!)
5. Z3 solved for minimal bundle:
   - Must include jimm (base)
   - Choose ONE database provider (postgresql preferred over mysql)
   - Choose ONE oauth provider (hydra preferred, reuses database)
   
Result: jimm + postgresql + hydra (3 apps, optimal)
Alternative bundles Z3 rejected:
- jimm + postgresql + keycloak (3 apps, but keycloak has lower priority)
- jimm + mysql + dex (3 apps, but mysql and dex have lower priority)
- jimm + postgresql + mysql + hydra (4 apps, unnecessary duplication)
""")
