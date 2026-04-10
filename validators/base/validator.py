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
from typing import Literal, Optional

import ops
from pydantic import BaseModel, Field

ValidationLevel = Literal["simple", "deep", "uat"]


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    message: str = ""


class ValidationResult(BaseModel):
    status: Literal["PASS", "FAIL", "ERROR", "SKIPPED"]
    endpoint: str
    interface: str
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

    def _skipped_result(self, level: ValidationLevel) -> ValidationResult:
        """Return a SKIPPED result indicating this validator does not support *level*."""
        return ValidationResult(
            status="SKIPPED",
            endpoint=self.endpoint,
            interface=self.interface,
            level=level,
            relation_id=self.relation_id,
            error=f"Level '{level}' is not supported by {self.__class__.__name__}.",
        )

    @abstractmethod
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        pass

    def relation_exists(self) -> bool:
        return self.relation.app is not None

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
