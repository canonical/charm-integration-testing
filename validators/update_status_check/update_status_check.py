# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Run the "simple"-level integration check used on every update-status hook.

This is a small, self-contained glue module: it discovers installed endpoint
validators via the standard setuptools `entry_points` (`endpoint_validators`
group, the same mechanism `validators-runner` uses) and runs them at the
"simple" level for every non-peer relation defined on the charm. It does not
depend on `validators-runner`, so charms using this module only need to
install the specific `validators-*` interface packages they actually use
(plus `validators-base`), instead of pulling in every interface validator in
this monorepo.
"""

import logging
from importlib.metadata import entry_points

from ops.charm import CharmBase
from ops.model import Relation
from pydantic import BaseModel

from validators.base import BaseValidator, ValidationResult, ValidationRole, str_to_validation_role

logger = logging.getLogger(__name__)


class UpdateStatusCheckResults(BaseModel):
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
    role: ValidationRole,
) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    for validator_cls in validators.get(interface_name, []):
        validator = validator_cls(charm, integration)
        logger.debug(
            f"Running validator '{validator_cls.__name__}' for endpoint '{integration.name}' "
            f"(interface='{interface_name}', role='{role}', level='simple')"
        )
        try:
            result = validator.validate(level="simple")
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
                    level="simple",
                    relation_id=integration.id,
                    error=f"Validator '{validator_cls.__name__}' raised an exception: {exc}",
                )
            )
    return results


def run_simple_check(charm: CharmBase) -> UpdateStatusCheckResults:
    """Run all installed validators for *charm* at the "simple" level.

    Logs an error for every FAIL/ERROR result. Returns the full results so
    the caller can build a unit status (e.g. from a collect-status handler)
    without needing to re-run the validators.
    """
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
                    level="simple",
                    relation_id=None,
                    error=f"Relation '{relation}' defined in metadata but not found in model.",
                )
            )
            continue
        for integration in charm.model.relations[relation]:
            results += _run_for_integration(validators, charm, interface_name, integration, role)

    for result in results:
        if result.status in ("FAIL", "ERROR"):
            failed_checks = "; ".join(c.message for c in result.checks if not c.passed)
            logger.error(
                "Integration check %s for endpoint '%s' (interface '%s', level '%s'): %s",
                result.status,
                result.endpoint,
                result.interface,
                result.level,
                result.error or failed_checks or "no details",
            )
    return UpdateStatusCheckResults(results=results)
