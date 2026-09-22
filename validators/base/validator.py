# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC, abstractmethod
from typing import Literal, Optional, cast, get_args

import ops
from pydantic import BaseModel, Field

ValidationLevel = Literal["simple", "deep", "uat"]
ValidationRole = Literal["requires", "provides", "peer"]
ValidationResultStatus = Literal["SKIPPED", "PASS", "FAIL", "ERROR"]


def str_to_validation_role(s: str) -> ValidationRole:
    if s not in get_args(ValidationRole):
        raise ValueError(f"Invalid validation role '{s}'. Must be one of {get_args(ValidationRole)}.")
    return cast(ValidationRole, s)


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    message: str = ""


class ValidationResult(BaseModel):
    status: ValidationResultStatus
    endpoint: str
    interface: str
    role: ValidationRole
    level: ValidationLevel
    relation_id: int
    checks: list[ValidationCheck] = Field(default_factory=list)
    error: Optional[str] = None


class PersistenceState(BaseModel):
    """Opaque cross-call state for a single persistence validator instance.

    ``id`` is a unique identifier chosen by the validator at ``prepare()`` time (not the Juju
    ``relation_id``, which is unstable across a relation remove/re-add). ``ref`` is a monotonically
    increasing counter the validator uses to assert no data was lost between calls.

    ``token`` is a random, unguessable value generated at ``prepare()`` time and written alongside
    the data the validator tracks. Validators must match records on ``token`` rather than on a value
    derived from ``id``/``ref``: those are reproducible, so a backend that loses the data and
    recreates it from scratch (resetting a sequence, for example) would otherwise still satisfy the
    check and report a false PASS. It is required and non-empty so a state that could not have come
    from ``prepare()`` is rejected rather than silently matching untagged records.
    """

    id: int
    ref: int = 0
    token: str = Field(min_length=1)


class PersistenceNotApplicable(Exception):
    """Raised by a ``BasePersistenceValidator`` method to signal it does not apply here.

    ``prepare()``/``checkpoint()``/``cleanup()`` have no ``SKIPPED`` result to return (unlike
    ``BaseValidator.validate()``), since ``prepare()``/``cleanup()`` don't produce a
    ``ValidationResult`` at all. Raise this instead - e.g. when ``self.role`` isn't the side of the
    relation persistence applies to - and callers treat it as "no applicable validator", not as a
    failure.
    """


class BaseValidator(ABC):
    charm: ops.CharmBase
    relation: ops.Relation

    def __init__(self, charm: ops.CharmBase, relation: ops.Relation) -> None:
        self.charm = charm
        self.relation = relation

    @property
    def role(self) -> ValidationRole:
        relation = self.charm.meta.relations[self.relation.name]
        return str_to_validation_role(relation.role.value)

    @property
    def endpoint(self) -> str:
        return self.relation.name

    @property
    def relation_id(self) -> int:
        return self.relation.id

    @property
    def databag(self) -> dict[str, str]:
        if self.relation.app not in self.relation.data:
            return {}
        return dict(self.relation.data[self.relation.app])

    @property
    def interface(self) -> str:
        return self.charm.meta.relations[self.relation.name].interface_name or ""

    def relation_exists(self) -> bool:
        return self.relation.app in self.relation.data

    def resolve_secret(self, uri_key: str, *fields: str, data: dict[str, str] | None = None) -> dict[str, str]:
        """Resolve credentials from *data* (default ``self.databag``) or the Juju secret it references."""
        source = self.databag if data is None else data
        if uri := source.get(uri_key):
            return self.charm.model.get_secret(id=uri).get_content()
        return {f: source[f] for f in fields if f in source}

    def validate_schema(
        self,
        required_fields: list[str],
        creds: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> ValidationCheck:
        """Check *required_fields* are present in *data* (default ``self.databag``), merged with *creds*."""
        merged = dict(self.databag if data is None else data)
        if creds:
            merged.update(creds)
        missing = [f for f in required_fields if not merged.get(f)]
        return ValidationCheck(
            name="schema",
            passed=not missing,
            message="OK" if not missing else f"Missing: {', '.join(missing)}",
        )

    def _make_result(
        self,
        *,
        level: ValidationLevel,
        status: ValidationResultStatus | None = None,
        checks: list[ValidationCheck] | None = None,
        error: str | None = None,
        endpoint: str | None = None,
        interface: str | None = None,
        role: ValidationRole | None = None,
        relation_id: int | None = None,
    ) -> ValidationResult:
        resolved_checks = [] if checks is None else checks
        if status is None:
            status = "PASS" if all(c.passed for c in resolved_checks) else "FAIL"
        return ValidationResult(
            status=status,
            endpoint=self.endpoint if endpoint is None else endpoint,
            interface=self.interface if interface is None else interface,
            role=role or self.role,
            level=level,
            relation_id=self.relation_id if relation_id is None else relation_id,
            checks=resolved_checks,
            error=error,
        )

    def _error_result(self, level: ValidationLevel, error: str) -> ValidationResult:
        return self._make_result(status="ERROR", level=level, checks=[], error=error)

    def _fail_result(self, level: ValidationLevel, checks: list[ValidationCheck]) -> ValidationResult:
        return self._make_result(status="FAIL", level=level, checks=checks)

    def _skipped_result_due_to_level(self, level: ValidationLevel) -> ValidationResult:
        """Return a SKIPPED result indicating this validator does not support *level*."""
        return self._make_result(
            status="SKIPPED",
            level=level,
            checks=[],
            error=f"Level '{level}' is not supported by {self.__class__.__name__}.",
        )

    def _skipped_result_due_to_role(self, level: ValidationLevel, role: ValidationRole) -> ValidationResult:
        """Return a SKIPPED result indicating this validator does not support *role*."""
        return self._make_result(
            status="SKIPPED",
            level=level,
            checks=[],
            error=f"Role '{role}' is not supported by {self.__class__.__name__}.",
        )

    @abstractmethod
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        pass


class BasePersistenceValidator(ABC):
    """Two-phase durability probe for a single relation, complementing ``BaseValidator``.

    Where ``BaseValidator.validate()`` is a stateless, idempotent health probe safe to call at any
    time, ``prepare()``/``checkpoint()`` form an explicitly stateful durability scenario: ``prepare()``
    seeds known data and establishes a steady state, and each subsequent passing ``checkpoint()``
    call verifies all previously-written data survived and advances the steady state for the next
    round. A failing ``checkpoint()`` leaves the steady state unchanged instead of advancing past
    the failure - see ``checkpoint()`` below. This is only meaningful when the caller (the test
    harness) controls the sequence and carries ``PersistenceState`` across calls; it is not a
    general-purpose health check.

    Concrete implementations choose how to map their internal storage (tables, keys, queues, ...)
    to the interface. They are not required to key storage off ``relation_id`` - it is not a stable
    identifier: it changes whenever a relation is removed and re-added, whereas the identifier
    chosen by ``prepare()`` (``PersistenceState.id``) is stable for the lifetime of the canary data
    it names.
    """

    charm: ops.CharmBase
    relation: ops.Relation

    def __init__(self, charm: ops.CharmBase, relation: ops.Relation) -> None:
        self.charm = charm
        self.relation = relation

    @property
    def role(self) -> ValidationRole:
        relation = self.charm.meta.relations[self.relation.name]
        return str_to_validation_role(relation.role.value)

    @property
    def endpoint(self) -> str:
        return self.relation.name

    @property
    def relation_id(self) -> int:
        return self.relation.id

    @property
    def databag(self) -> dict[str, str]:
        if self.relation.app not in self.relation.data:
            return {}
        return dict(self.relation.data[self.relation.app])

    @property
    def interface(self) -> str:
        return self.charm.meta.relations[self.relation.name].interface_name or ""

    def relation_exists(self) -> bool:
        return self.relation.app in self.relation.data

    def resolve_secret(self, uri_key: str, *fields: str, data: dict[str, str] | None = None) -> dict[str, str]:
        """Resolve credentials from *data* (default ``self.databag``) or the Juju secret it references."""
        source = self.databag if data is None else data
        if uri := source.get(uri_key):
            return self.charm.model.get_secret(id=uri).get_content()
        return {f: source[f] for f in fields if f in source}

    def validate_schema(
        self,
        required_fields: list[str],
        creds: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> ValidationCheck:
        """Check *required_fields* are present in *data* (default ``self.databag``), merged with *creds*."""
        merged = dict(self.databag if data is None else data)
        if creds:
            merged.update(creds)
        missing = [f for f in required_fields if not merged.get(f)]
        return ValidationCheck(
            name="schema",
            passed=not missing,
            message="OK" if not missing else f"Missing: {', '.join(missing)}",
        )

    def _make_result(
        self,
        *,
        level: ValidationLevel,
        status: ValidationResultStatus | None = None,
        checks: list[ValidationCheck] | None = None,
        error: str | None = None,
        endpoint: str | None = None,
        interface: str | None = None,
        role: ValidationRole | None = None,
        relation_id: int | None = None,
    ) -> ValidationResult:
        resolved_checks = [] if checks is None else checks
        if status is None:
            status = "PASS" if all(c.passed for c in resolved_checks) else "FAIL"
        return ValidationResult(
            status=status,
            endpoint=self.endpoint if endpoint is None else endpoint,
            interface=self.interface if interface is None else interface,
            role=role or self.role,
            level=level,
            relation_id=self.relation_id if relation_id is None else relation_id,
            checks=resolved_checks,
            error=error,
        )

    def _error_result(self, level: ValidationLevel, error: str) -> ValidationResult:
        return self._make_result(status="ERROR", level=level, checks=[], error=error)

    def _fail_result(self, level: ValidationLevel, checks: list[ValidationCheck]) -> ValidationResult:
        return self._make_result(status="FAIL", level=level, checks=checks)

    @abstractmethod
    def prepare(self) -> PersistenceState:
        """Write known test data through the relation. Called before a disruptive operation."""

    @abstractmethod
    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        """Verify all prior data is still present. On PASS, write a new marker and return the
        advanced state; on FAIL, return ``expected`` unchanged and write nothing - the harness
        only carries a returned state forward when the result is a PASS (see
        ``ValidatorRunner.checkpoint_all``), so writing/advancing on FAIL would drift the backend
        past what the harness will ever compare against again, masking the original failure
        instead of letting a later checkpoint re-detect it."""

    @abstractmethod
    def cleanup(self) -> None:
        """Remove all canary data written by this validator instance."""
