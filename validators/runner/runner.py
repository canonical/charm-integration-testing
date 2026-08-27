# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import get_args

import ops
from ops.charm import CharmBase
from ops.framework import Framework
from ops.model import _ModelBackend
from ops.storage import SQLiteStorage
from pydantic import BaseModel

from validators.base import BaseValidator, ValidationLevel, ValidationResult
from validators.engine import load_validators, run_for_charm

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
        return load_validators()

    def run(self, charm: CharmBase, level: ValidationLevel) -> ValidatorRunnerResults:
        logger.info(f"Running validators at level '{level}'")
        results = run_for_charm(charm, level=level, validators=self.validators)
        logger.info(f"Finished running validators at level '{level}': {len(results)} result(s)")
        return ValidatorRunnerResults(results=results)


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
