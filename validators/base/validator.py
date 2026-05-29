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
    status: Literal["PASS", "FAIL", "ERROR", "SKIPPED"]
    endpoint: str
    interface: str
    role: ValidationRole
    level: ValidationLevel
    relation_id: int
    checks: list[ValidationCheck] = Field(default_factory=list)
    error: Optional[str] = None


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
        if self.relation.app is None:
            return {}
        return dict(self.relation.data[self.relation.app])

    @property
    def interface(self) -> str:
        return self.charm.meta.relations[self.relation.name].interface_name or ""

    def _skipped_result_due_to_level(self, level: ValidationLevel) -> ValidationResult:
        """Return a SKIPPED result indicating this validator does not support *level*."""
        return self._make_result(
            status="SKIPPED",
            level=level,
            checks=[],
            error=f"Level '{level}' is not supported by {self.__class__.__name__}.",
        )

    @abstractmethod
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        pass

    def relation_exists(self) -> bool:
        return self.relation.app in self.relation.data

    def resolve_secret(self, uri_key: str, *fields: str) -> dict[str, str]:
        if uri := self.databag.get(uri_key):
            return self.charm.model.get_secret(id=uri).get_content()
        return {f: self.databag[f] for f in fields if f in self.databag}

    def validate_schema(self, required_fields: list[str], creds: dict[str, str] | None = None) -> ValidationCheck:
        data = self.databag.copy()
        if creds:
            data.update(creds)
        missing = [f for f in required_fields if not data.get(f)]
        return ValidationCheck(
            name="schema",
            passed=not missing,
            message="OK" if not missing else f"Missing: {', '.join(missing)}",
        )

    def _make_result(
        self,
        status: ValidationResultStatus,
        level: ValidationLevel,
        checks: list[ValidationCheck] | None = None,
        error: str | None = None,
        endpoint: str | None = None,
        interface: str | None = None,
        role: ValidationRole | None = None,
        relation_id: int | None = None,
    ) -> ValidationResult:
        return ValidationResult(
            status=status,
            endpoint=self.endpoint if endpoint is None else endpoint,
            interface=self.interface if interface is None else interface,
            role=role or self.role,
            level=level,
            relation_id=self.relation_id if relation_id is None else relation_id,
            checks=[] if checks is None else checks,
            error=error,
        )

    def _error_result(self, level: ValidationLevel, error: str) -> ValidationResult:
        return self._make_result("ERROR", level, [], error)

    def _fail_result(self, level: ValidationLevel, checks: list[ValidationCheck]) -> ValidationResult:
        return self._make_result("FAIL", level, checks)
