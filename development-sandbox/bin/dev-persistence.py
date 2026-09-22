#!/usr/bin/env python3
# Drive the persistence lifecycle (prepare/checkpoint/cleanup) against a deployed Juju application.
#
# This script is designed to run INSIDE the sandbox VM.
#
# Usage:
#   dev-persistence.py --app <app> --op prepare [--model <model>] [--controller <controller>]
#                      [--state-file <path>] [--reinstall]
#
# The persistence lifecycle is stateful: prepare() seeds canary data and returns a
# PersistenceState per relation, checkpoint() verifies that data survived a disruption and
# advances the state, and cleanup() drops the canary data. This script threads that state
# through a JSON state file so the three ops can be run as separate invocations - which is
# what a disruption between prepare and checkpoint requires.

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Derive project root from this script's location: <project>/development-sandbox/bin/dev-persistence.py
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


def resolve_controller(explicit: str | None) -> str:
    """Return the given controller, or fall back to Juju's active controller."""
    if explicit:
        return explicit
    out = subprocess.run(
        ["juju", "whoami", "--format=json"],
        capture_output=True,
        text=True,
        check=True,
    )
    data: dict[str, str] = json.loads(out.stdout)
    controller = data.get("controller")
    if not controller:
        print("ERROR: no active Juju controller; pass --controller explicitly", file=sys.stderr)
        sys.exit(1)
    return controller


def load_state(path: Path) -> dict[Any, Any]:
    """Load the persisted (PersistenceKey -> PersistenceState) mapping, or {} if absent."""
    from charm_integration_testing.juju.models import PersistenceKey
    from validators.base import PersistenceState

    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {
        PersistenceKey(
            controller=entry["controller"],
            model=entry["model"],
            unit=entry["unit"],
            relation_id=entry["relation_id"],
        ): PersistenceState.model_validate(entry["state"])
        for entry in raw
    }


def save_state(path: Path, state: dict[Any, Any]) -> None:
    """Persist the (PersistenceKey -> PersistenceState) mapping as a stable, sorted JSON list."""
    entries = [
        {
            "controller": key.controller,
            "model": key.model,
            "unit": key.unit,
            "relation_id": key.relation_id,
            "state": value.model_dump(),
        }
        for key, value in sorted(
            state.items(),
            key=lambda kv: (kv[0].controller, kv[0].model, kv[0].unit, kv[0].relation_id),
        )
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2) + "\n")


def print_results(results: dict[str, list[Any]]) -> None:
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
    parser = argparse.ArgumentParser(
        description="Drive the persistence lifecycle (prepare/checkpoint/cleanup) on a Juju application's units."
    )
    parser.add_argument("--model", default="testing", help="Juju model name (default: testing)")
    parser.add_argument(
        "--controller",
        default=None,
        help="Juju controller the model lives on (default: current active controller)",
    )
    parser.add_argument(
        "--app", required=True, help="Application name to run persistence ops on (e.g. postgresql-client)"
    )
    parser.add_argument(
        "--op",
        required=True,
        choices=["prepare", "checkpoint", "cleanup"],
        help="Persistence lifecycle operation to run",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help=(
            "JSON file threading PersistenceState between ops (default: "
            "<project>/development-sandbox/.persistence-state-<model>-<app>.json). "
            "prepare writes it, checkpoint reads and advances it, cleanup reads and prunes it."
        ),
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
    logger = logging.getLogger("dev-persistence")

    state_file = (
        Path(args.state_file)
        if args.state_file
        else PROJECT_ROOT / "development-sandbox" / f".persistence-state-{args.model}-{args.app}.json"
    )

    try:
        from charm_integration_testing.extensions.validator_injection.extension import (
            ValidatorInjectorExtension,
            remote_validators_path,
        )
        from charm_integration_testing.juju import JujuModelHandle
        from charm_integration_testing.juju_jubilant.backend import JubilantBackend
    except ImportError as exc:
        print(f"ERROR: Could not import project packages: {exc}", file=sys.stderr)
        print("Run 'scripts/sandbox.sh up' to set up the Python venv.", file=sys.stderr)
        sys.exit(1)

    backend = JubilantBackend()
    model = JujuModelHandle(controller=resolve_controller(args.controller), model=args.model)

    if args.reinstall:
        logger.info("Removing remote validator venv on all units of '%s'...", args.app)
        is_k8s = backend.is_k8s_model(model)
        rm_cmd = f"rm -rf {remote_validators_path}" if is_k8s else f"sudo rm -rf {remote_validators_path}"
        for unit in backend.application_units(model, args.app):
            logger.debug("  %s on %s", rm_cmd, unit)
            backend.ssh(model, unit, rm_cmd)

    uv_file = STATIC_UV if STATIC_UV.exists() else None
    extension = ValidatorInjectorExtension(
        validators_path=VALIDATORS_DIR,
        juju=backend,
        logger=logger,
        uv_file=uv_file,
    )

    persistence_state = load_state(state_file)
    logger.info(
        "Running persistence op '%s' on %s (model=%s, tracked relations=%d)...",
        args.op,
        args.app,
        args.model,
        len(persistence_state),
    )
    results = extension.post_persistence(model, args.app, args.op, persistence_state)
    save_state(state_file, persistence_state)
    logger.info("Persistence state written to %s", state_file)
    print_results(results)


if __name__ == "__main__":
    main()
