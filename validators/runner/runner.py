# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import json
import logging
import os
import sys
from importlib.metadata import entry_points
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, TypeVar, get_args

import ops
from ops.charm import CharmBase
from ops.framework import Framework
from ops.model import Relation, _ModelBackend
from ops.storage import SQLiteStorage
from pydantic import BaseModel, Field, ValidationError

from validators.base import (
    BasePersistenceValidator,
    BaseValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationLevel,
    ValidationResult,
    ValidationRole,
    str_to_validation_role,
)

# Persistence lifecycle operations accepted by --persistence.
PersistenceOp = str  # "prepare" | "checkpoint" | "cleanup"
_PERSISTENCE_OPS = ("prepare", "checkpoint", "cleanup")

# Level assigned to persistence ValidationResults: checkpoint() is a read/write probe, so "deep"
# is the closest existing fit (there's no separate persistence level in ValidationLevel).
_PERSISTENCE_RESULT_LEVEL: ValidationLevel = "deep"

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

_T = TypeVar("_T")

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
    # Updated PersistenceState per relation_id (as a string key, matching --refs), populated by
    # --persistence prepare/checkpoint. Empty for functional-only runs and for cleanup (which has
    # no state to carry forward - see BasePersistenceValidator.cleanup).
    updated_refs: dict[str, PersistenceState] = Field(default_factory=dict)
    # relation_ids that --persistence cleanup actually visited (i.e. had a live relation with a
    # registered persistence validator at cleanup time), whether or not the cleanup call itself
    # succeeded. Callers use this to distinguish "cleanup ran for this relation" (safe to drop
    # tracked state, once absent from any FAIL/ERROR result) from "this relation_id was never
    # touched" (e.g. the relation was already removed), which must keep its tracked state so
    # orphaned canary data isn't forgotten.
    cleaned_relation_ids: list[int] = Field(default_factory=list)


class ValidatorRunner:
    validators: dict[str, list[type[BaseValidator]]]
    persistence_validators: dict[str, list[type[BasePersistenceValidator]]]
    # Interface names whose endpoint_persistence_validators entry point failed to load, mapped to
    # the error message. Consulted by prepare_all/checkpoint_all/cleanup_all so a currently
    # connected relation on one of these interfaces surfaces as an ERROR result instead of
    # silently looking like "no persistence validator applicable" - which cleanup_all in
    # particular must not treat as a successful (and therefore state-clearing) cleanup.
    persistence_load_errors: dict[str, str]

    def __init__(self) -> None:
        self.validators = self._load_validators()
        self.persistence_load_errors = {}
        self.persistence_validators = self._load_persistence_validators()

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

    def _load_persistence_validators(self) -> dict[str, list[type[BasePersistenceValidator]]]:
        validators: dict[str, list[type[BasePersistenceValidator]]] = {}
        for ep in entry_points(group="endpoint_persistence_validators"):
            try:
                validator_cls = ep.load()
                if not issubclass(validator_cls, BasePersistenceValidator):
                    raise TypeError(
                        f"entry point '{ep.name}' does not implement BasePersistenceValidator "
                        f"(got {validator_cls!r})"
                    )
                # Unlike functional validators, persistence state (PersistenceState per relation_id
                # in --refs/updated_refs) has no room to distinguish which validator a state entry
                # belongs to, so two persistence validators on the same interface would silently
                # overwrite each other's state. Warn loudly rather than misbehaving quietly.
                if ep.name in validators:
                    logger.warning(
                        f"Multiple persistence validators registered for interface '{ep.name}'; only one "
                        "PersistenceState is tracked per relation_id, so their prepare()/checkpoint() calls "
                        "will overwrite each other's state. This is unsupported - register at most one "
                        "persistence validator per interface."
                    )
                validators.setdefault(ep.name, []).append(validator_cls)
            except Exception as exc:
                logger.exception(f"Failed to load persistence validator for '{ep.name}'")
                # Record the failure so prepare_all/checkpoint_all/cleanup_all can surface an
                # ERROR result for any relation on this interface, instead of silently treating
                # it the same as "no persistence validator registered".
                self.persistence_load_errors[ep.name] = str(exc)
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

    def _iter_persistence_targets(self, charm: CharmBase) -> list[tuple[Relation, str, ValidationRole]]:
        """Non-peer (integration, interface_name, role) triples with a registered persistence validator."""
        targets: list[tuple[Relation, str, ValidationRole]] = []
        for relation_name, metadata in charm.meta.relations.items():
            if (role := str_to_validation_role(metadata.role.name)) == "peer":
                continue
            interface_name = metadata.interface_name or relation_name
            if interface_name not in self.persistence_validators:
                continue
            for integration in charm.model.relations.get(relation_name, []):
                targets.append((integration, interface_name, role))
        return targets

    def _persistence_missing_relation_results(self, charm: CharmBase) -> list[ValidationResult]:
        """ERROR results for persistence interfaces whose metadata relation has no live relation.

        ``_iter_persistence_targets`` iterates the relations present in the model, so a metadata
        relation that is not (yet) established yields no target and is silently skipped. Reporting
        an ERROR instead means a ``prepare`` on a partially initialized model cannot look like a
        success whose following checkpoint trivially passes with nothing validated.

        Only interfaces with a registered persistence validator (or a load error) are considered;
        for any other interface there is no persistence validation to skip.
        """
        results: list[ValidationResult] = []
        for relation_name, metadata in charm.meta.relations.items():
            if (role := str_to_validation_role(metadata.role.name)) == "peer":
                continue
            interface_name = metadata.interface_name or relation_name
            if interface_name not in self.persistence_validators and interface_name not in self.persistence_load_errors:
                continue
            if charm.model.relations.get(relation_name):
                continue
            error = f"Relation '{relation_name}' defined in metadata but not found in model."
            logger.error(error)
            results.append(
                ValidationResult(
                    status="ERROR",
                    endpoint=relation_name,
                    interface=interface_name,
                    role=role,
                    level=_PERSISTENCE_RESULT_LEVEL,
                    relation_id=-1,
                    error=error,
                )
            )
        return results

    def _persistence_load_error_results(self, charm: CharmBase) -> list[ValidationResult]:
        """ERROR results for every live relation on an interface whose persistence validator failed to load.

        Without this, a relation on such an interface is indistinguishable from one with no
        persistence validator registered at all: both are silently absent from
        ``_iter_persistence_targets``. That ambiguity is unsafe for cleanup, whose caller treats an
        empty result list as "nothing to clean up" and deletes tracked state.
        """
        if not self.persistence_load_errors:
            return []
        results: list[ValidationResult] = []
        for relation_name, metadata in charm.meta.relations.items():
            if str_to_validation_role(metadata.role.name) == "peer":
                continue
            interface_name = metadata.interface_name or relation_name
            error = self.persistence_load_errors.get(interface_name)
            if error is None:
                continue
            for integration in charm.model.relations.get(relation_name, []):
                results.append(
                    ValidationResult(
                        status="ERROR",
                        endpoint=relation_name,
                        interface=interface_name,
                        role=str_to_validation_role(metadata.role.name),
                        level=_PERSISTENCE_RESULT_LEVEL,
                        relation_id=integration.id,
                        error=f"Persistence validator for interface '{interface_name}' failed to load: {error}",
                    )
                )
        return results

    def _find_relation_by_id(self, charm: CharmBase, relation_id: int) -> tuple[Relation, str, ValidationRole] | None:
        """Locate a live, non-peer relation by its Juju relation_id, along with its interface and role.

        Peer relations are skipped, matching ``_iter_persistence_targets``/
        ``_persistence_load_error_results``: persistence validators are only registered against
        non-peer interfaces, so a stale or malformed ``--refs`` entry that happens to collide with
        a peer relation's ``relation_id`` must not resolve to it.
        """
        for relation_name, metadata in charm.meta.relations.items():
            role = str_to_validation_role(metadata.role.name)
            if role == "peer":
                continue
            interface_name = metadata.interface_name or relation_name
            for integration in charm.model.relations.get(relation_name, []):
                if integration.id == relation_id:
                    return integration, interface_name, role
        return None

    def prepare_all(self, charm: CharmBase) -> ValidatorRunnerResults:
        """Seed canary data on every relation with a registered persistence validator.

        Makes no assertions; each validator's returned ``PersistenceState`` is collected, keyed by
        the relation's Juju ``relation_id`` (as a string, matching the ``--refs`` wire format).
        """
        logger.info("Preparing persistence validators")
        results: list[ValidationResult] = self._persistence_missing_relation_results(charm)
        results += self._persistence_load_error_results(charm)
        updated_refs: dict[str, PersistenceState] = {}
        for integration, interface_name, role in self._iter_persistence_targets(charm):
            for validator_cls in self.persistence_validators[interface_name]:
                state, error_result, skipped = self._call_persistence_method(
                    validator_cls, charm, integration, interface_name, role, lambda v: v.prepare()
                )
                if error_result is not None:
                    results.append(error_result)
                elif skipped:
                    continue
                elif not isinstance(state, PersistenceState):
                    # The abstract contract requires prepare() to return a PersistenceState; a
                    # validator returning anything else leaves this relation with no tracked state,
                    # which would silently skip every later checkpoint for it rather than
                    # surfacing the broken implementation.
                    logger.error(
                        f"Persistence validator '{validator_cls.__name__}' for endpoint "
                        f"'{integration.name}' returned {state!r} from prepare() instead of a "
                        "PersistenceState."
                    )
                    results.append(
                        ValidationResult(
                            status="ERROR",
                            endpoint=integration.name,
                            interface=interface_name,
                            role=role,
                            level=_PERSISTENCE_RESULT_LEVEL,
                            relation_id=integration.id,
                            error=(
                                f"Persistence validator '{validator_cls.__name__}' returned "
                                f"{state!r} from prepare() instead of a PersistenceState."
                            ),
                        )
                    )
                else:
                    updated_refs[str(integration.id)] = state
        logger.info(f"Finished preparing persistence validators: {len(updated_refs)} relation(s) seeded")
        return ValidatorRunnerResults(results=results, updated_refs=updated_refs)

    def checkpoint_all(self, charm: CharmBase, refs: dict[str, PersistenceState]) -> ValidatorRunnerResults:
        """Verify all previously-seeded canary data is still present for every ref in *refs*."""
        logger.info(f"Checkpointing persistence validators for {len(refs)} relation(s)")
        results: list[ValidationResult] = self._persistence_missing_relation_results(charm)
        results += self._persistence_load_error_results(charm)
        updated_refs: dict[str, PersistenceState] = {}
        for relation_id_str, expected in refs.items():
            try:
                relation_id = int(relation_id_str)
            except ValueError:
                # A malformed key means the tracked state for this entry can never be
                # checkpointed; report it as an ERROR rather than silently discarding it, which
                # would let a real durability check pass without ever running.
                logger.error(f"Invalid relation_id '{relation_id_str}' in --refs; cannot checkpoint.")
                results.append(
                    ValidationResult(
                        status="ERROR",
                        endpoint="",
                        interface="",
                        role="requires",
                        level=_PERSISTENCE_RESULT_LEVEL,
                        relation_id=-1,
                        error=f"Invalid relation_id '{relation_id_str}' in --refs; cannot checkpoint.",
                    )
                )
                continue
            found = self._find_relation_by_id(charm, relation_id)
            if found is None:
                logger.error(f"Relation id {relation_id} not found in model; cannot checkpoint.")
                results.append(
                    ValidationResult(
                        status="ERROR",
                        endpoint="",
                        interface="",
                        role="requires",
                        level=_PERSISTENCE_RESULT_LEVEL,
                        relation_id=relation_id,
                        error=f"Relation id {relation_id} not found in model; cannot checkpoint.",
                    )
                )
                continue
            integration, interface_name, role = found
            registered_validators = self.persistence_validators.get(interface_name, [])
            if not registered_validators:
                if interface_name in self.persistence_load_errors:
                    # _persistence_load_error_results() (seeded into `results` above) already
                    # added an ERROR for every live relation on this interface, including this
                    # one - don't add a second, more generic ERROR for the same relation_id.
                    continue
                # A ref was supplied for a relation whose interface has no loaded persistence
                # validator. This can't come from this run's prepare_all (which only tracks
                # interfaces it found validators for), so the ref is stale (e.g. the charm's
                # interface changed) - silently doing nothing would let a real durability check
                # pass without ever running.
                logger.error(
                    f"No persistence validator registered for interface '{interface_name}'; cannot checkpoint."
                )
                results.append(
                    ValidationResult(
                        status="ERROR",
                        endpoint=integration.name,
                        interface=interface_name,
                        role=role,
                        level=_PERSISTENCE_RESULT_LEVEL,
                        relation_id=relation_id,
                        error=f"No persistence validator registered for interface '{interface_name}'; cannot checkpoint.",
                    )
                )
                continue
            for validator_cls in registered_validators:
                outcome, error_result, skipped = self._call_persistence_method(
                    validator_cls, charm, integration, interface_name, role, lambda v: v.checkpoint(expected)
                )
                if error_result is not None:
                    results.append(error_result)
                elif skipped:
                    continue
                elif not (
                    isinstance(outcome, tuple)
                    and len(outcome) == 2
                    and isinstance(outcome[0], ValidationResult)
                    and isinstance(outcome[1], PersistenceState)
                ):
                    # The abstract contract requires checkpoint() to return a
                    # (ValidationResult, PersistenceState) pair; anything else would otherwise be
                    # silently treated the same as a legitimate PersistenceNotApplicable skip,
                    # hiding a broken implementation and never checkpointing this relation again.
                    logger.error(
                        f"Persistence validator '{validator_cls.__name__}' for endpoint "
                        f"'{integration.name}' returned {outcome!r} from checkpoint() instead of a "
                        "(ValidationResult, PersistenceState) pair."
                    )
                    results.append(
                        ValidationResult(
                            status="ERROR",
                            endpoint=integration.name,
                            interface=interface_name,
                            role=role,
                            level=_PERSISTENCE_RESULT_LEVEL,
                            relation_id=relation_id,
                            error=(
                                f"Persistence validator '{validator_cls.__name__}' returned "
                                f"{outcome!r} from checkpoint() instead of a "
                                "(ValidationResult, PersistenceState) pair."
                            ),
                        )
                    )
                else:
                    result, new_state = outcome
                    results.append(result)
                    # Only carry the new state forward on PASS: checkpoint() can return an advanced
                    # state alongside a FAIL/ERROR result when the underlying check didn't hold.
                    # Recording that anyway would overwrite the last-known-good baseline before the
                    # caller raises, so a later retry could checkpoint against post-failure state
                    # and spuriously pass.
                    if result.status == "PASS":
                        updated_refs[relation_id_str] = new_state
        logger.info(f"Finished checkpointing persistence validators: {len(results)} result(s)")
        return ValidatorRunnerResults(results=results, updated_refs=updated_refs)

    def cleanup_all(self, charm: CharmBase) -> ValidatorRunnerResults:
        """Drop all canary data for every relation with a registered persistence validator."""
        logger.info("Cleaning up persistence validators")
        results: list[ValidationResult] = self._persistence_missing_relation_results(charm)
        results += self._persistence_load_error_results(charm)
        cleaned_relation_ids: list[int] = []
        for integration, interface_name, role in self._iter_persistence_targets(charm):
            # Only report the relation as cleaned once a cleanup() invocation actually ran. A
            # PersistenceNotApplicable skip means no canary data was dropped, so reporting it as
            # cleaned would make post_persistence() forget tracked state for a relation whose
            # cleanup never ran. Ordinary errors still count as "ran", so post_persistence()'s
            # FAIL/ERROR filtering preserves the tracked state for them.
            cleanup_ran = False
            for validator_cls in self.persistence_validators[interface_name]:
                _, error_result, skipped = self._call_persistence_method(
                    validator_cls, charm, integration, interface_name, role, lambda v: v.cleanup()
                )
                if error_result is not None:
                    results.append(error_result)
                if not skipped:
                    cleanup_ran = True
            if cleanup_ran:
                cleaned_relation_ids.append(integration.id)
        logger.info("Finished cleaning up persistence validators")
        return ValidatorRunnerResults(results=results, updated_refs={}, cleaned_relation_ids=cleaned_relation_ids)

    def _call_persistence_method(
        self,
        validator_cls: type[BasePersistenceValidator],
        charm: CharmBase,
        integration: Relation,
        interface_name: str,
        role: ValidationRole,
        call: "Callable[[BasePersistenceValidator], _T]",
    ) -> tuple[_T | None, ValidationResult | None, bool]:
        """Instantiate *validator_cls* and invoke *call* on it, translating outcomes uniformly.

        Returns ``(value, None, False)`` on success, ``(None, error_result, False)`` if the
        validator raised, or ``(None, None, True)`` if it raised ``PersistenceNotApplicable`` (a
        silent skip, not an error). The third element distinguishes that skip from a validator
        returning ``None``/an invalid value on success, which prepare_all/checkpoint_all must treat
        as an ERROR rather than silently discard.
        """
        try:
            validator = validator_cls(charm, integration)
            return call(validator), None, False
        except PersistenceNotApplicable:
            logger.debug(
                f"Persistence validator '{validator_cls.__name__}' for endpoint '{integration.name}' "
                "is not applicable to this relation side; skipping."
            )
            return None, None, True
        except Exception as exc:
            logger.exception(
                f"Persistence validator '{validator_cls.__name__}' for endpoint '{integration.name}' raised an exception"
            )
            return (
                None,
                ValidationResult(
                    status="ERROR",
                    endpoint=integration.name,
                    interface=interface_name,
                    role=role,
                    level=_PERSISTENCE_RESULT_LEVEL,
                    relation_id=integration.id,
                    error=f"Persistence validator '{validator_cls.__name__}' raised an exception: {exc}",
                ),
                False,
            )


def _parse_cli_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, dict[str, PersistenceState]]:
    """Parse and validate CLI args, including the ``--refs`` JSON payload.

    Split out from :func:`main` so the argument-parsing/validation logic (the ``--level``
    zero-flag default, the ``--refs``-required-for-checkpoint check, and ``--refs`` JSON
    decoding) can be unit tested without needing a real ``JUJU_CHARM_DIR``/ops ``Model``.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--level", default=None, choices=get_args(ValidationLevel))
    parser.add_argument("--persistence", default=None, choices=_PERSISTENCE_OPS)
    parser.add_argument(
        "--refs",
        default=None,
        help=(
            'JSON dict of {"<relation_id>": {"id": <identifier>, "ref": <ref>, "token": <token>}}, '
            "matching the state returned by --persistence prepare. Required for --persistence checkpoint."
        ),
    )
    args = parser.parse_args(argv)

    if args.persistence == "checkpoint" and args.refs is None:
        parser.error("--refs is required when --persistence checkpoint is used")

    if args.refs is not None and args.persistence != "checkpoint":
        # --refs is only meaningful for checkpoint (prepare_all/cleanup_all take no state
        # argument). Without this check, a typo such as "--persistence prepare --refs ..." would
        # parse and validate the JSON but then silently ignore it.
        parser.error("--refs is only valid when --persistence checkpoint is used")

    # Preserve the pre-persistence CLI contract: invoking run_validators with no flags at all
    # still runs the "simple" functional level, matching every existing direct caller. Only
    # suppress the functional run when --persistence was explicitly requested (a persistence-only
    # invocation), so --level and --persistence can still be combined or used independently.
    if args.level is None and args.persistence is None:
        args.level = "simple"

    refs: dict[str, PersistenceState] = {}
    if args.refs is not None:
        try:
            raw_refs = json.loads(args.refs)
            refs = {key: PersistenceState.model_validate(value) for key, value in raw_refs.items()}
        except (json.JSONDecodeError, ValidationError, AttributeError) as exc:
            parser.error(f"Invalid --refs JSON: {exc}")

    return args, refs


def main() -> None:
    _configure_logging()

    args, refs = _parse_cli_args()

    logger.info(f"Starting validator run (level={args.level!r}, persistence={args.persistence!r})")

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
        results = ValidatorRunnerResults(results=[])

        if args.level is not None:
            level_results = runner.run(charm, level=args.level)
            results.results.extend(level_results.results)

        if args.persistence == "prepare":
            persistence_results = runner.prepare_all(charm)
        elif args.persistence == "checkpoint":
            persistence_results = runner.checkpoint_all(charm, refs)
        elif args.persistence == "cleanup":
            persistence_results = runner.cleanup_all(charm)
        else:
            persistence_results = None

        if persistence_results is not None:
            results.results.extend(persistence_results.results)
            results.updated_refs.update(persistence_results.updated_refs)
            results.cleaned_relation_ids.extend(persistence_results.cleaned_relation_ids)
    finally:
        framework.close()

    # Output results as JSON
    print(results.model_dump_json())


if __name__ == "__main__":
    main()
