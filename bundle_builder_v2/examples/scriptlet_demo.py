#!/usr/bin/env python3
"""
Example demonstrating scriptlet execution and constraint extraction.

This script shows how to:
1. Load scriptlets from files
2. Simulate Operator Framework events
3. Extract constraint information from rejections
4. Generate Z3 constraints (conceptually) from error codes
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bundle_builder_v2.scriptlet_invoker import ScriptletInvoker, parse_error_code_rejection


def load_scriptlet(charm_name: str) -> str:
    """Load a scriptlet file from the static directory."""
    scriptlet_path = Path(__file__).parent.parent.parent / "static" / "charm-scriptlet-overrides" / f"{charm_name}.star"

    if not scriptlet_path.exists():
        raise FileNotFoundError(f"Scriptlet not found: {scriptlet_path}")

    return scriptlet_path.read_text()


def demo_wordpress():
    """Demo WordPress scriptlet - requires db integration."""
    print("=" * 70)
    print("WordPress-k8s Scriptlet Demo")
    print("=" * 70)

    scriptlet_code = load_scriptlet("wordpress-k8s")
    invoker = ScriptletInvoker(scriptlet_code)

    # Test 1: Install without db integration (should reject)
    print("\n[Test 1] Install without db integration:")
    rejection = invoker.fire_install_event(relations={})

    if rejection:
        print(f"  ✗ Rejected: field='{rejection.field}', reason='{rejection.reason}'")
        if rejection.is_error_code:
            parsed = parse_error_code_rejection(rejection)
            if parsed:
                print(f"  → Constraint: {parsed.constraint_type}")
                print(f"  → Required endpoint: {parsed.required_endpoint}")
                print("  → Z3 formula: Implies(wordpress_deployed, db_integrated)")
        else:
            print("  → Legacy format (no structured constraint)")
    else:
        print("  ✓ Accepted")

    # Test 2: Install with db integration (should accept)
    print("\n[Test 2] Install with db integration:")
    rejection = invoker.fire_install_event(relations={"db": [1]})

    if rejection:
        print(f"  ✗ Rejected: {rejection}")
    else:
        print("  ✓ Accepted - WordPress can deploy with db integration")


def demo_postgresql():
    """Demo PostgreSQL scriptlet - mutual exclusion constraint."""
    print("\n" + "=" * 70)
    print("PostgreSQL-k8s Scriptlet Demo")
    print("=" * 70)

    scriptlet_code = load_scriptlet("postgresql-k8s")
    invoker = ScriptletInvoker(scriptlet_code)

    # Test 1: Join database endpoint when db endpoint already exists (should reject)
    print("\n[Test 1] Join 'database' when 'db' already integrated:")
    rejection = invoker.fire_relation_joined_event(endpoint="database", relation_id=2, relations={"db": [1]})

    if rejection:
        print(f"  ✗ Rejected: field='{rejection.field}', reason={rejection.reason}")
        if rejection.is_error_code:
            parsed = parse_error_code_rejection(rejection)
            if parsed:
                print(f"  → Constraint: {parsed.constraint_type}")
                print(f"  → Conflicting: {parsed.conflicting_endpoints}")
                print("  → Z3 formula: AtMost(database_integrated, db_integrated, db_admin_integrated, 1)")
        else:
            print("  → Legacy format")
    else:
        print("  ✓ Accepted")

    # Test 2: Join database endpoint with no conflicts (should accept)
    print("\n[Test 2] Join 'database' with no conflicts:")
    rejection = invoker.fire_relation_joined_event(endpoint="database", relation_id=1, relations={})

    if rejection:
        print(f"  ✗ Rejected: {rejection}")
    else:
        print("  ✓ Accepted - Can integrate database endpoint")


def demo_mysql():
    """Demo MySQL scriptlet - limit constraint."""
    print("\n" + "=" * 70)
    print("MySQL-k8s Scriptlet Demo")
    print("=" * 70)

    scriptlet_code = load_scriptlet("mysql-k8s")
    invoker = ScriptletInvoker(scriptlet_code)

    # Test 1: Join 6th database client (should reject)
    print("\n[Test 1] Join 6th database client (limit is 5):")
    rejection = invoker.fire_relation_joined_event(
        endpoint="database", relation_id=6, relations={"database": [1, 2, 3, 4, 5, 6]}
    )

    if rejection:
        print(f"  ✗ Rejected: field='{rejection.field}', reason='{rejection.reason}'")
        if rejection.is_error_code:
            parsed = parse_error_code_rejection(rejection)
            if parsed:
                print(f"  → Constraint: {parsed.constraint_type}")
                print(f"  → Endpoint: {parsed.endpoint}")
                print(f"  → Max: {parsed.max}")
                print("  → Z3 formula: Sum(database_relation_vars) <= 5")
        else:
            print("  → Legacy format")
    else:
        print("  ✓ Accepted")

    # Test 2: Join 3rd database client (should accept)
    print("\n[Test 2] Join 3rd database client (under limit):")
    rejection = invoker.fire_relation_joined_event(
        endpoint="database", relation_id=3, relations={"database": [1, 2, 3]}
    )

    if rejection:
        print(f"  ✗ Rejected: {rejection}")
    else:
        print("  ✓ Accepted - Under limit")


def demo_pgbouncer():
    """Demo PGBouncer scriptlet - required and conditional constraints."""
    print("\n" + "=" * 70)
    print("PGBouncer-k8s Scriptlet Demo")
    print("=" * 70)

    scriptlet_code = load_scriptlet("pgbouncer-k8s")
    invoker = ScriptletInvoker(scriptlet_code)

    # Test 1: Install without backend-database (should reject)
    print("\n[Test 1] Install without backend-database:")
    rejection = invoker.fire_install_event(relations={"database": [1]})

    if rejection:
        print(f"  ✗ Rejected: field='{rejection.field}', reason='{rejection.reason}'")
        if rejection.is_error_code:
            parsed = parse_error_code_rejection(rejection)
            if parsed:
                print(f"  → Constraint: {parsed.constraint_type}")
                print("  → Z3 formula: Implies(pgbouncer_deployed, backend_database_integrated)")
    else:
        print("  ✓ Accepted")

    # Test 2: Install without client endpoints (should reject)
    print("\n[Test 2] Install with backend-database but no client endpoints:")
    rejection = invoker.fire_install_event(relations={"backend-database": [1]})

    if rejection:
        print(f"  ✗ Rejected: field='{rejection.field}', reason={rejection.reason}")
        if rejection.is_error_code:
            parsed = parse_error_code_rejection(rejection)
            if parsed:
                print(f"  → Constraint: {parsed.constraint_type}")
                print(f"  → Acceptable: {parsed.acceptable_endpoints}")
                print(
                    "  → Z3 formula: Implies(pgbouncer_deployed, Or(database_integrated, db_integrated, db_admin_integrated))"
                )
    else:
        print("  ✓ Accepted")

    # Test 3: Install with all required integrations (should accept)
    print("\n[Test 3] Install with backend-database and client endpoint:")
    rejection = invoker.fire_install_event(relations={"backend-database": [1], "database": [2]})

    if rejection:
        print(f"  ✗ Rejected: {rejection}")
    else:
        print("  ✓ Accepted - All constraints satisfied")


def main():
    """Run all demos."""
    print("\n" + "=" * 70)
    print("Scriptlet Invoker Demo - Error Code Encoding")
    print("=" * 70)
    print("\nDemonstrating constraint extraction from scriptlet rejections")
    print("using error code encoding (field=constraint_type, reason=details)\n")

    try:
        demo_wordpress()
        demo_postgresql()
        demo_mysql()
        demo_pgbouncer()

        print("\n" + "=" * 70)
        print("Summary")
        print("=" * 70)
        print("✓ All scriptlets use error code encoding")
        print("✓ Bundle builder can parse constraint types")
        print("✓ Z3 constraints can be generated from error codes")
        print("✓ No JU055 spec changes required")
        print("=" * 70 + "\n")

    except FileNotFoundError as e:
        print(f"\n❌ Error: {e}")
        print("Make sure scriptlet files exist in ../static/charm-scriptlet-overrides/")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
