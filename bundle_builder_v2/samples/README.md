# Bundle Builder v2 - Z3 Constraint Solver Approach

This directory contains prototypes demonstrating how to use Z3 (SMT solver) to automatically construct valid Juju bundles that satisfy complex integration constraints.

## Key Concepts

### Problem: Bundle Building as Constraint Satisfaction
Given:
- Base graph: some applications that must be included
- Charm metadata: endpoints (provides/requires) for each charm
- Integration rules: constraints on how endpoints can connect
- Goal: Find minimal valid bundle (fewest apps/integrations) that satisfies all rules

### Solution: Encode as Z3 SMT Problem
1. **Variables**: Boolean for each potential app/integration
2. **Hard constraints**: Must-have rules (interface matching, provides→requires, conditional requirements)
3. **Soft constraints**: Minimize apps/integrations, prioritize certain node types
4. **Solver**: Z3 finds satisfying assignment (valid bundle) optimizing objective

## Files

- `example_charm_metadata.py` - Mock charm metadata structure
- `z3_basic_example.py` - Simple Z3 encoding showing core concepts
- `z3_advanced_example.py` - Full prototype with all constraint types
- `z3_dynamic_exploration.py` - Discover providers dynamically (simulated Charmhub) and let Z3 choose
- `z3_capacity_instances.py` - Capacity constraints and multi-instance providers (e.g., multiple PostgreSQL units)
- `constraint_types.md` - Detailed explanation of each constraint type and Z3 encoding

## Constraint Types Supported

1. **Interface matching**: `edge(app1.endpoint1, app2.endpoint2) → interface(endpoint1) == interface(endpoint2)`
2. **Directionality**: `edge(provides_ep, requires_ep) only, not reversed`
3. **Same application**: `edge(app1.ep1, X) → edge(app1.ep2, X)` (both endpoints to same app)
4. **Channel matching**: `edge(app1, app2) → channel(app1) == channel(app2)`
5. **Conditional requirements**: `edge(app.ep1, _) → edge(app.ep2, _)` (if ep1 integrated, ep2 must be)
6. **Mutual exclusion**: `edge(app.ep1, _) → ¬edge(app.ep2, _)` (if ep1 integrated, ep2 cannot be)
7. **Bridges/transitivity**: `certs_from(A,C) ↔ edge(A,B) ∧ provides_certs(B,C)`

## Next Steps

Run the examples to see how Z3 constructs valid bundles automatically.
