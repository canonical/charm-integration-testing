# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import argparse
import logging
import os
import sys
from importlib.metadata import entry_points
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import get_args

import ops
from ops.charm import CharmBase
from ops.framework import Framework
from ops.model import Relation, _ModelBackend
from ops.storage import SQLiteStorage
from pydantic import BaseModel

from validators.base import (
    BaseValidator,
    ValidationLevel,
    ValidationResult,
    ValidationRole,
    str_to_validation_role,
)

# Ordered from highest to lowest; each level falls back to the next entry.
_LEVEL_FALLBACK: dict[ValidationLevel, ValidationLevel | None] = {
    "uat": "deep",
    "deep": "simple",
    "simple": None,
}

# Log location on the unit. Deliberately under /var/log so it gets picked up by
# juju-crashdump / juju-k8s-crashdump collection alongside other unit logs.
LOG_DIR = Path("/var/log/validators")
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

logger = logging.getLogger("validators")

# Name used to tag handlers this module installs, so _configure_logging() can tell them
# apart from handlers a host application may have already attached to the same logger.
_MANAGED_HANDLER_NAME = "validators-runner-managed-handler"


def _configure_logging(log_dir: Path = LOG_DIR) -> None:
    """Configure the "validators" logger to write to <log_dir>/validator.log.

    stdout is reserved for the final JSON results blob (parsed by the caller), so this
    logger never attaches a stream handler there. If the log directory/file can't be
    created or written to (permission error, read-only fs, etc.), fall back to logging to
    stderr only - logging problems must never prevent validation from running.

    Args:
        log_dir: Directory to write the log file into. Injectable so callers (and tests)
            can point logging elsewhere without touching module state.
    """
    logger.setLevel(logging.DEBUG)
    # Never propagate to the root logger: some hosts (e.g. ops/charm frameworks) attach a
    # stdout stream handler there, which would leak log records into the JSON-only stdout.
    logger.propagate = False
    # Idempotent: clear only handlers this function previously installed (identified by
    # name), so re-invoking it doesn't duplicate log lines but also doesn't clobber
    # handlers a host application may have attached to this logger itself.
    for existing_handler in list(logger.handlers):
        if existing_handler.name == _MANAGED_HANDLER_NAME:
            logger.removeHandler(existing_handler)
            existing_handler.close()

    log_file = log_dir / "validator.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        handler = logging.StreamHandler(sys.stderr)
        handler.set_name(_MANAGED_HANDLER_NAME)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.warning(f"Could not set up logging to {log_file}, falling back to stderr: {exc}")
        return

    handler.set_name(_MANAGED_HANDLER_NAME)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class ValidatorRunnerResults(BaseModel):
    results: list[ValidationResult]


class ValidatorRunner:
    validators: dict[str, list[type[BaseValidator]]]

    def __init__(self) -> None:
        self.validators = self._load_validators()

    @staticmethod
    def _load_validators() -> dict[str, list[type[BaseValidator]]]:
        validators: dict[str, list[type[BaseValidator]]] = {}
        for ep in entry_points(group="endpoint_validators"):
            try:
                validator_cls = ep.load()
                if not issubclass(validator_cls, BaseValidator):
                    logger.warning(f"Entry point '{ep.name}' does not implement BaseValidator. Skipping.")
                    continue
                validators.setdefault(ep.name, []).append(validator_cls)
            except Exception:
                logger.exception(f"Failed to load validator for '{ep.name}'")
        return validators

    def run(self, charm: CharmBase, level: ValidationLevel) -> ValidatorRunnerResults:
        logger.info(f"Running validators at level '{level}'")
        # Get the list of endpoints
        results = []
        for relation, metadata in charm.meta.relations.items():
            if (role := str_to_validation_role(metadata.role.name)) == "peer":
                continue
            interface_name = metadata.interface_name or relation

            if relation not in charm.model.relations:
                logger.error(f"Relation '{relation}' defined in metadata but not found in model.")
                results.append(
                    ValidationResult(
                        status="ERROR",
                        endpoint=relation,
                        interface=interface_name,
                        role=role,
                        level=level,
                        relation_id=None,
                        error=f"Relation '{relation}' defined in metadata but not found in model.",
                    )
                )
                continue
            for integration in charm.model.relations[relation]:
                results += self._run_for_integration(charm, interface_name, integration, level, role)
        logger.info(f"Finished running validators at level '{level}': {len(results)} result(s)")
        return ValidatorRunnerResults(results=results)

    def _run_for_integration(
        self,
        charm: CharmBase,
        interface_name: str,
        integration: Relation,
        level: ValidationLevel,
        role: ValidationRole,
    ) -> list[ValidationResult]:
        results: list[ValidationResult] = []
        for validator_cls in self.validators.get(interface_name, []):
            validator = validator_cls(charm, integration)
            logger.debug(
                f"Running validator '{validator_cls.__name__}' for endpoint '{integration.name}' "
                f"(interface='{interface_name}', role='{role}', level='{level}')"
            )
            try:
                result = validator.validate(level=level)
                # If the validator doesn't support this level, fall back to the
                # next lower level until we either get a real result or exhaust
                # all options and surface the final SKIPPED.
                while result.status == "SKIPPED":
                    fallback = _LEVEL_FALLBACK[result.level]
                    if fallback is None:
                        break
                    result = validator.validate(level=fallback)
                logger.debug(
                    f"Validator '{validator_cls.__name__}' for endpoint '{integration.name}' "
                    f"finished with status '{result.status}'"
                )
                results.append(result)
            except Exception as exc:
                logger.exception(
                    f"Validator '{validator_cls.__name__}' for endpoint '{integration.name}' raised an exception"
                )
                results.append(
                    ValidationResult(
                        status="ERROR",
                        endpoint=integration.name,
                        interface=interface_name,
                        role=role,
                        level=level,
                        relation_id=integration.id,
                        error=f"Validator '{validator_cls.__name__}' raised an exception: {exc}",
                    )
                )
        return results


def main() -> None:
    _configure_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("--level", default="simple", choices=get_args(ValidationLevel))
    args = parser.parse_args()
    logger.info(f"Starting validator run (level='{args.level}')")

    # Load validators
    runner = ValidatorRunner()

    # Set up the Ops framework to access the model and secrets
    charm_dir = Path(os.environ["JUJU_CHARM_DIR"])
    backend = _ModelBackend()
    metadata = ops.CharmMeta.from_yaml((charm_dir / "metadata.yaml").read_text())
    model = ops.Model(metadata, backend)
    storage = SQLiteStorage(":memory:")
    framework = Framework(storage, charm_dir, metadata, model)

    # Run validators and collect results
    try:
        charm = CharmBase(framework)
        results = runner.run(charm, level=args.level)
    finally:
        framework.close()

    # Output results as JSON
    print(results.model_dump_json())


if __name__ == "__main__":
    main()
