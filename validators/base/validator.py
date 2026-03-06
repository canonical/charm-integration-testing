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
    status: Literal["PASS", "FAIL", "ERROR"]
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

    @abstractmethod
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        pass
