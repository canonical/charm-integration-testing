#!/usr/bin/env python3
# Inject and run validators against a deployed Juju application.
#
# This script is designed to run INSIDE the sandbox VM.
#
# Usage:
#   dev-validate --app <app> [--model <model>] [--level simple] [--reinstall]
#
# --reinstall  Delete the remote validator venv before injecting, forcing a
#              full reinstall. Use this after editing validator source code.

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

# Derive project root from this script's location: <project>/development-sandbox/bin/dev-validate.py
PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", str(Path(__file__).parent.parent.parent)))

# If not already running inside the project's Poetry venv, re-exec via
# "poetry run python3" so all project packages (charm_integration_testing,
# validators, etc.) are importable without manual PYTHONPATH juggling.
_POETRY_MARKER = "pypoetry"
if _POETRY_MARKER not in sys.executable:
    _result = subprocess.run(
        ["poetry", "run", "python3"] + sys.argv,
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )
    sys.exit(_result.returncode)
VALIDATORS_DIR = PROJECT_ROOT / "validators"

# Pre-built uv binary for SCP'ing to units (avoids downloading on each run).
STATIC_UV = PROJECT_ROOT / "static" / "uv"


def print_results(results: dict) -> None:
    for unit, unit_results in results.items():
        print(f"\n{'='*60}")
        print(f"Unit: {unit}")
        print(f"{'='*60}")
        for r in unit_results:
            sym = {"PASS": "PASS", "FAIL": "FAIL", "ERROR": "ERR ", "SKIPPED": "SKIP"}.get(r.status, r.status)
            print(f"  [{sym}] {r.interface} / {r.endpoint}  (relation {r.relation_id}, level={r.level})")
            if r.error:
                print(f"         error: {r.error}")
            for check in r.checks:
                mark = "ok" if check.passed else "!!"
                msg = f": {check.message}" if check.message else ""
                print(f"         [{mark}] {check.name}{msg}")

    raw = {unit: [r.model_dump() for r in rs] for unit, rs in results.items()}
    print("\n--- JSON ---")
    print(json.dumps(raw, indent=2))

    all_results = [r for rs in results.values() for r in rs]
    if any(r.status in ("FAIL", "ERROR") for r in all_results):
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject and run validators on a Juju application's units.")
    parser.add_argument("--model", default="testing", help="Juju model name (default: testing)")
    parser.add_argument("--app", required=True, help="Application name to validate (e.g. postgresql-k8s)")
    parser.add_argument(
        "--level",
        default="simple",
        choices=["simple", "deep", "uat"],
        help="Validation level (default: simple)",
    )
    parser.add_argument(
        "--reinstall",
        action="store_true",
        help="Force reinstall validators on units (use after editing validator code)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    logger = logging.getLogger("dev-validate")

    try:
        from charm_integration_testing.extensions.validator_injection.extension import (
            ValidatorInjectorExtension,
        )
        from charm_integration_testing.juju_jubilant.backend import JubilantBackend
    except ImportError as exc:
        print(f"ERROR: Could not import project packages: {exc}", file=sys.stderr)
        print("Run 'scripts/sandbox.sh up' to set up the Python venv.", file=sys.stderr)
        sys.exit(1)

    backend = JubilantBackend()

    if args.reinstall:
        logger.info("Removing remote validator venv on all units of '%s'...", args.app)
        is_k8s = backend.is_k8s_model(args.model)
        rm_cmd = "rm -rf /var/lib/validators" if is_k8s else "sudo rm -rf /var/lib/validators"
        for unit in backend.application_units(args.model, args.app):
            logger.debug("  %s on %s", rm_cmd, unit)
            backend.ssh(args.model, unit, rm_cmd)

    uv_file = STATIC_UV if STATIC_UV.exists() else None
    extension = ValidatorInjectorExtension(
        validators_path=VALIDATORS_DIR,
        juju=backend,
        logger=logger,
        uv_file=uv_file,
    )

    logger.info("Running validators on %s (model=%s, level=%s)...", args.app, args.model, args.level)
    results = extension.post_validate(args.model, args.app, args.level)
    print_results(results)


if __name__ == "__main__":
    main()
