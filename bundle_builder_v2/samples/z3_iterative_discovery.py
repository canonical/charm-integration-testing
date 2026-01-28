a"""
Iterative bundle building with Z3 unsat_core for interface discovery.

Instead of pre-computing worst-case bounds, we solve iteratively:
1. Start with minimal bundle (just consumers)
2. Try to solve
3. If UNSAT, examine unsat_core to find missing interfaces
4. Query for providers of missing interfaces
5. Add provider instances and retry
6. Repeat until SAT

Key insight: Use tracked assertions to make unsat_core human-readable.
"""

from z3 import Bool, Or, Implies, Optimize, sat, unsat, And, Not
from typing import Dict, List, Set, Optional


class MockCharmMetadata:
    """Simplified charm metadata"""
    def __init__(self, name: str, provides: Dict[str, str], requires: Dict[str, str]):
        self.name = name
        self.provides = provides  # {interface_name: interface_type}
        self.requires = requires  # {endpoint_name: interface_type}


class MockCharmhub:
    """Mock Charmhub API - simulates searching for charms by interface"""
    
    CHARMS = {
        'postgresql': MockCharmMetadata(
            'postgresql',
            provides={'db': 'postgresql_client'},
            requires={}
        ),
        'pgbouncer': MockCharmMetadata(
            'pgbouncer',
            provides={'db': 'postgresql_client'},
            requires={'postgres': 'postgresql_client'}
        ),
        'oauth2-proxy': MockCharmMetadata(
            'oauth2-proxy',
            provides={'oauth': 'oauth'},
            requires={'database': 'postgresql_client'}
        ),
        'hydra': MockCharmMetadata(
            'hydra',
            provides={},
            requires={'db': 'postgresql_client', 'cookies': 'oauth'}
        ),
    }
    
    @staticmethod
    def search_providers(interface_type: str) -> List[str]:
        """Find all charms that provide this interface"""
        providers = []
        for charm_name, metadata in MockCharmhub.CHARMS.items():
            if any(iface == interface_type for iface in metadata.provides.values()):
                providers.append(charm_name)
        return providers
    
    @staticmethod
    def get_metadata(charm_name: str) -> Optional[MockCharmMetadata]:
        return MockCharmhub.CHARMS.get(charm_name)


def iterative_bundle_builder():
    """
    Build bundle iteratively using Z3 unsat_core for interface discovery.
    
    Start with: hydra (requires oauth, postgresql_client)
    Iteration 1: UNSAT → need oauth and postgresql_client providers
    Iteration 2: Add oauth2-proxy, postgresql → UNSAT (oauth2-proxy needs postgresql_client)
    Iteration 3: Already have postgresql → SAT
    """
    
    print("=" * 70)
    print("ITERATIVE BUNDLE BUILDING WITH UNSAT_CORE")
    print("=" * 70)
    
    # Starting point: just the consumer
    bundle = {'hydra': MockCharmhub.get_metadata('hydra')}
    iteration = 0
    
    while True:
        iteration += 1
        print(f"\n{'─' * 70}")
        print(f"ITERATION {iteration}")
        print(f"{'─' * 70}")
        print(f"Current bundle: {list(bundle.keys())}")
        
        # Build Z3 solver for current bundle
        solver = Optimize()
        app_vars = {}
        
        # Track which apps are in bundle
        for app_name in bundle.keys():
            app_vars[app_name] = Bool(f'app_{app_name}')
            solver.add(app_vars[app_name] == True)
        
        # For each requirement, assert it must be satisfied
        unsatisfied_interfaces: Set[str] = set()
        
        for app_name, metadata in bundle.items():
            for endpoint_name, interface_type in metadata.requires.items():
                # This requirement must be satisfied by some app in the bundle
                providers = []
                for provider_name, provider_meta in bundle.items():
                    if any(iface == interface_type for iface in provider_meta.provides.values()):
                        providers.append(provider_name)
                
                if providers:
                    # At least one provider exists
                    provider_vars = [app_vars[p] for p in providers]
                    constraint = Or(provider_vars)
                    solver.assert_and_track(
                        constraint,
                        f'{app_name}:{endpoint_name}:requires:{interface_type}'
                    )
                else:
                    # No provider exists → we know we need this interface
                    unsatisfied_interfaces.add(interface_type)
                    print(f"\n✗ {app_name} requires '{interface_type}' (endpoint: {endpoint_name})")
                    print(f"  → No provider in bundle yet")
        
        # If we already identified missing interfaces, skip to solving
        if not unsatisfied_interfaces:
            # Try to solve
            result = solver.check()
            
            if result == sat:
                print(f"\n✓ BUNDLE SATISFIED!")
                print(f"\nFinal bundle:")
                for app_name in bundle.keys():
                    print(f"  - {app_name}")
                
                print(f"\nIntegrations:")
                for app_name, metadata in bundle.items():
                    for endpoint_name, interface_type in metadata.requires.items():
                        providers = []
                        for provider_name, provider_meta in bundle.items():
                            if any(iface == interface_type for iface in provider_meta.provides.values()):
                                providers.append(provider_name)
                        if providers:
                            print(f"  - {app_name} ({endpoint_name}) ← {providers[0]}")
                
                return bundle
            
            elif result == unsat:
                # Get unsat_core to find conflicting assertions
                core = solver.unsat_core()
                print(f"\n✗ UNSATISFIABLE (core has {len(core)} assertions)")
                
                # Parse unsat_core to extract interface requirements
                for assertion in core:
                    print(f"  - {assertion}")
                    # Extract interface from assertion name
                    if ':requires:' in str(assertion):
                        parts = str(assertion).split(':')
                        if len(parts) >= 4:
                            interface_type = parts[3]
                            unsatisfied_interfaces.add(interface_type)
        
        # Query Charmhub for missing interfaces
        if unsatisfied_interfaces:
            print(f"\n→ Need to provide: {', '.join(unsatisfied_interfaces)}")
            
            for interface_type in unsatisfied_interfaces:
                providers = MockCharmhub.search_providers(interface_type)
                
                if not providers:
                    print(f"✗ No charms provide '{interface_type}' interface!")
                    return None
                
                # Add the first available provider that's not already in bundle
                for provider_name in providers:
                    if provider_name not in bundle:
                        metadata = MockCharmhub.get_metadata(provider_name)
                        bundle[provider_name] = metadata
                        print(f"✓ Added {provider_name} to provide '{interface_type}'")
                        break
        else:
            # No more unsatisfied interfaces and solver is UNSAT?
            # This shouldn't happen - indicates a logic error
            print("ERROR: No unsatisfied interfaces but solver is UNSAT!")
            return None
        
        if iteration > 10:
            print("ERROR: Too many iterations, infinite loop?")
            return None


if __name__ == "__main__":
    bundle = iterative_bundle_builder()
    
    print("\n" + "=" * 70)
    print("KEY INSIGHTS")
    print("=" * 70)
    print("""
1. Start minimal: Just the consumer app
2. Each iteration: Build Z3 solver with current bundle
3. Check satisfiability:
   - SAT → done!
   - UNSAT → examine unsat_core
4. Parse unsat_core to identify missing interfaces
5. Query Charmhub for providers
6. Add provider instances and retry
7. Natural convergence: Only adds what's needed

Advantages:
- No pessimistic worst-case bounds
- Unsat_core directly tells us what's missing
- Minimal bundle by construction
- Naturally discovers chains (e.g., pgbouncer → postgresql)
""")
