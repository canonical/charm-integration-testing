# Copyright (C) 2026 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from dataclasses import dataclass

import pytest

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult


@dataclass
class RelationStub:
    name: str
    id: int


class ConcreteValidator(BaseValidator):
    """Minimal concrete implementation for testing BaseValidator."""

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="PASS",
            endpoint=self.endpoint,
            interface="test-interface",
            level=level,
            relation_id=self.relation_id,
        )


class TestValidationCheck:
    def test_message_defaults_to_empty_string(self) -> None:
        # GIVEN / WHEN
        check = ValidationCheck(name="schema", passed=False)

        # THEN
        assert check.message == ""


class TestValidationResult:
    def test_serialises_to_json(self) -> None:
        # GIVEN
        result = ValidationResult(
            status="PASS",
            endpoint="db",
            interface="postgresql_client",
            level="simple",
            relation_id=1,
        )

        # WHEN
        json_str = result.model_dump_json()

        # THEN
        assert '"status":"PASS"' in json_str
        assert '"interface":"postgresql_client"' in json_str


class TestBaseValidator:
    def test_validate_returns_result(self) -> None:
        # GIVEN
        validator = ConcreteValidator(object(), RelationStub(name="my-db", id=0))  # type: ignore[arg-type]

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert isinstance(result, ValidationResult)
        assert result.status == "PASS"
        assert result.level == "simple"

    def test_cannot_instantiate_abstract_class(self) -> None:
        # GIVEN / WHEN / THEN
        with pytest.raises(TypeError):
            BaseValidator(object(), RelationStub(name="x", id=0))  # type: ignore[abstract, arg-type]
