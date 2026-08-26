# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared engine for discovering and running endpoint validators.

This module holds the validator-discovery, relation-iteration, and
level-fallback logic common to every caller of the Interface Validators
Framework: the CLI `validators-runner`, and the in-charm
`validators-update-status-check` / `validators-validate-action` glue
packages. It is deliberately kept separate from `validators-base` (the
contract every interface validator package depends on - `BaseValidator`,
`ValidationResult`, etc.) since orchestration is a different concern from
defining what a validator is; only the three packages above need it.
"""

import logging
from importlib.metadata import entry_points

from ops.charm import CharmBase
from ops.model import Relation

from validators.base.validator import (
    BaseValidator,
    ValidationLevel,
    ValidationResult,
    ValidationRole,
    str_to_validation_role,
)

logger = logging.getLogger(__name__)

# Ordered from highest to lowest; each level falls back to the next entry.
LEVEL_FALLBACK: dict[ValidationLevel, ValidationLevel | None] = {
    "uat": "deep",
    "deep": "simple",
    "simple": None,
}


def load_validators() -> dict[str, list[type[BaseValidator]]]:
    """Discover installed endpoint validators via the `endpoint_validators` entry-point group."""
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


def run_for_integration(
    validators: dict[str, list[type[BaseValidator]]],
    charm: CharmBase,
    interface_name: str,
    integration: Relation,
    level: ValidationLevel,
    role: ValidationRole,
) -> list[ValidationResult]:
    """Run every validator registered for *interface_name* against *integration* at *level*.

    Falls back to progressively lower levels if a validator returns SKIPPED at the
    requested level, surfacing the final SKIPPED result if no level is supported.
    """
    results: list[ValidationResult] = []
    for validator_cls in validators.get(interface_name, []):
        validator = validator_cls(charm, integration)
        logger.debug(
            f"Running validator '{validator_cls.__name__}' for endpoint '{integration.name}' "
            f"(interface='{interface_name}', role='{role}', level='{level}')"
        )
        try:
            result = validator.validate(level=level)
            while result.status == "SKIPPED":
                fallback = LEVEL_FALLBACK[result.level]
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


def run_for_charm(
    charm: CharmBase,
    level: ValidationLevel,
    validators: dict[str, list[type[BaseValidator]]] | None = None,
) -> list[ValidationResult]:
    """Run all installed validators for *charm* at *level*, across every non-peer relation.

    *validators* is discovered via `load_validators()` if not supplied; callers that run
    validators repeatedly (e.g. the CLI runner) can load them once and pass the result in.
    """
    if validators is None:
        validators = load_validators()

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
            results += run_for_integration(validators, charm, interface_name, integration, level, role)
    return results
