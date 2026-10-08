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
from collections.abc import Iterable
from importlib.metadata import entry_points

from ops.charm import CharmBase
from ops.model import Application, Relation, Unit

from validators.base import (
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


def _has_data(integration: Relation, app: Application | None, units: Iterable[Unit]) -> bool:
    """Return True if *app* or any of *units* has published data on *integration*."""
    if app is not None and bool(dict(integration.data.get(app, {}))):
        return True
    return any(bool(dict(integration.data.get(unit, {}))) for unit in units)


def _has_negotiated_data(integration: Relation, charm: CharmBase) -> bool:
    """Return True once either side has published data on *integration*.

    Which side a validator reads is validator-specific rather than derivable
    from the relation role, so treat the relation as ready once either side
    has published anything; the gate can then never get stuck at SKIPPED.
    """
    return _has_data(integration, charm.app, [charm.unit]) or _has_data(integration, integration.app, integration.units)


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
        try:
            validator = validator_cls(charm, integration)
            logger.debug(
                f"Running validator '{validator_cls.__name__}' for endpoint '{integration.name}' "
                f"(interface='{interface_name}', role='{role}', level='{level}')"
            )
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
    skip_missing_unvalidated: bool = False,
) -> list[ValidationResult]:
    """Run all installed validators for *charm* at *level*, across every non-peer relation.

    *validators* is discovered via `load_validators()` if not supplied; callers that run
    validators repeatedly (e.g. the CLI runner) can load them once and pass the result in.

    A relation declared in metadata but absent from the model is reported as an ERROR
    by default, preserving the CLI runner's historical behavior. Callers that only
    want to validate installed validators can set *skip_missing_unvalidated* to skip
    missing relations with no installed validator, as well as relations explicitly
    marked optional. The same flag also skips (as SKIPPED, not FAIL/ERROR) integrations
    that exist in the model but have not negotiated data yet, so periodic in-charm
    callers (update-status, `validate`) don't need their own readiness gate.
    """
    if validators is None:
        validators = load_validators()

    results: list[ValidationResult] = []
    for relation, metadata in charm.meta.relations.items():
        if (role := str_to_validation_role(metadata.role.name)) == "peer":
            continue
        interface_name = metadata.interface_name or relation

        if relation not in charm.model.relations:
            if skip_missing_unvalidated:
                if metadata.optional:
                    logger.debug(f"Optional relation '{relation}' not found in model; skipping.")
                    continue
                if interface_name not in validators:
                    logger.debug(
                        f"Relation '{relation}' not found in model and no validator is installed "
                        f"for interface '{interface_name}'; skipping."
                    )
                    continue
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
            if (
                skip_missing_unvalidated
                and interface_name in validators
                and not _has_negotiated_data(integration, charm)
            ):
                logger.debug(
                    f"Relation '{relation}' (id={integration.id}) has not negotiated data yet; skipping until it is ready."
                )
                results.append(
                    ValidationResult(
                        status="SKIPPED",
                        endpoint=relation,
                        interface=interface_name,
                        role=role,
                        level=level,
                        relation_id=integration.id,
                        error="Relation has not negotiated data yet.",
                    )
                )
                continue
            results += run_for_integration(validators, charm, interface_name, integration, level, role)
    return results
