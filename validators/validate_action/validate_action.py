# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Run endpoint validators at a user-chosen level, for a charm's `validate` action.

This is a small, self-contained glue module: it discovers installed endpoint
validators via the standard setuptools `entry_points` (`endpoint_validators`
group, the same mechanism `validators-runner` uses) and runs them, for every
non-peer relation defined on the charm, at the level requested via the
action's `level` param. It does not depend on `validators-runner`, so charms
using this module only need to install the specific `validators-*` interface
packages they actually use (plus `validators-base`), instead of pulling in
every interface validator in this monorepo.
"""

import logging
from importlib.metadata import entry_points
from typing import get_args

from ops.charm import ActionEvent, CharmBase
from ops.model import Relation
from pydantic import BaseModel

from validators.base import (
    BaseValidator,
    ValidationLevel,
    ValidationResult,
    ValidationRole,
    str_to_validation_role,
)

logger = logging.getLogger(__name__)

# Ordered from highest to lowest; each level falls back to the next entry.
_LEVEL_FALLBACK: dict[ValidationLevel, ValidationLevel | None] = {
    "uat": "deep",
    "deep": "simple",
    "simple": None,
}


class ValidateActionResults(BaseModel):
    results: list[ValidationResult]


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


def _run_for_integration(
    validators: dict[str, list[type[BaseValidator]]],
    charm: CharmBase,
    interface_name: str,
    integration: Relation,
    level: ValidationLevel,
    role: ValidationRole,
) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    for validator_cls in validators.get(interface_name, []):
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


def _run(charm: CharmBase, level: ValidationLevel) -> ValidateActionResults:
    validators = _load_validators()
    results: list[ValidationResult] = []
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
            results += _run_for_integration(validators, charm, interface_name, integration, level, role)
    return ValidateActionResults(results=results)


def run_validate_action(charm: CharmBase, event: ActionEvent) -> None:
    """Handle the `validate` action.

    Reads the `level` action param (defaults to "simple"), runs all
    installed validators for *charm* at that level, reports the full
    results as JSON via `event.set_results`, and fails the action if any
    result is `ERROR`.
    """
    level = event.params.get("level", "simple")
    if level not in get_args(ValidationLevel):
        event.fail(f"Invalid level '{level}'. Must be one of {get_args(ValidationLevel)}.")
        return

    results = _run(charm, level=level)

    statuses = [result.status for result in results.results]
    event.set_results(
        {
            "results": results.model_dump_json(),
            "summary": (
                f"pass={statuses.count('PASS')} "
                f"fail={statuses.count('FAIL')} "
                f"error={statuses.count('ERROR')} "
                f"skipped={statuses.count('SKIPPED')}"
            ),
        }
    )

    errors = [r for r in results.results if r.status == "ERROR"]
    if errors:
        summary = "; ".join(f"{r.endpoint} ({r.interface}): {r.error}" for r in errors)
        event.fail(f"{len(errors)} validator(s) raised an error: {summary}")
