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
import os
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import get_args

import ops
from ops.charm import CharmBase
from ops.framework import Framework
from ops.model import Relation, _ModelBackend
from ops.storage import SQLiteStorage
from pydantic import BaseModel

from validators.base import BaseValidator, ValidationLevel, ValidationResult

# Ordered from highest to lowest; each level falls back to the next entry.
_LEVEL_FALLBACK: dict[ValidationLevel, ValidationLevel | None] = {
    "uat": "deep",
    "deep": "simple",
    "simple": None,
}


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
                    print(f"Entry point '{ep.name}' does not implement BaseValidator. Skipping.", file=sys.stderr)
                    continue
                validators.setdefault(ep.name, []).append(validator_cls)
            except Exception as exc:
                print(f"Failed to load validator for '{ep.name}': {exc}", file=sys.stderr)
        return validators

    def run(self, charm: CharmBase, level: ValidationLevel) -> ValidatorRunnerResults:
        # Get the list of requires endpoints
        results = []
        for required_endpoint, endpoint_metadata in charm.meta.requires.items():
            interface_name = endpoint_metadata.interface_name or required_endpoint
            for integration in charm.model.relations[required_endpoint]:
                results += self._run_for_integration(charm, interface_name, integration, level)
        return ValidatorRunnerResults(results=results)

    def _run_for_integration(
        self, charm: CharmBase, interface_name: str, integration: Relation, level: ValidationLevel
    ) -> list[ValidationResult]:
        results: list[ValidationResult] = []
        for validator_cls in self.validators.get(interface_name, []):
            validator = validator_cls(charm, integration)
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
                results.append(result)
            except Exception as exc:
                results.append(
                    ValidationResult(
                        status="ERROR",
                        endpoint=integration.name,
                        interface=interface_name,
                        level=level,
                        relation_id=integration.id,
                        error=f"Validator '{validator_cls.__name__}' raised an exception: {exc}",
                    )
                )
        return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--level", default="simple", choices=get_args(ValidationLevel))
    args = parser.parse_args()

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
