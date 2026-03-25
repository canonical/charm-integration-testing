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


class AppStub:
    """Minimal stand-in for ops.Application used as a relation-data key."""


@dataclass
class RelationStub:
    name: str
    id: int
    app: AppStub | None = None
    data: dict[AppStub | None, dict[str, str]] | None = None


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


def _make_validator(databag: dict[str, str] | None = None) -> ConcreteValidator:
    app = AppStub()
    relation_databag = databag or {}
    relation = RelationStub(name="db", id=1, app=app, data={app: relation_databag})
    return ConcreteValidator(object(), relation)  # type: ignore[arg-type]


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

    def test_databag_returns_copy_of_remote_app_databag(self) -> None:
        # GIVEN
        validator = _make_validator(databag={"database": "mydb"})

        # WHEN
        databag = validator.databag
        databag["database"] = "other"

        # THEN
        assert validator.databag["database"] == "mydb"

    def test_schema_validation_check_passes_when_required_fields_exist(self) -> None:
        # GIVEN
        validator = _make_validator()

        # WHEN
        check = validator._schema_validation_check(
            required_fields=["endpoints", "database", "username", "password"],
            data={
                "endpoints": "10.0.0.1:5432",
                "database": "mydb",
                "username": "user",
                "password": "pass",
            },
        )

        # THEN
        assert check.passed
        assert check.message == "OK"

    def test_schema_validation_check_fails_for_missing_or_empty_fields(self) -> None:
        # GIVEN
        validator = _make_validator()

        # WHEN
        check = validator._schema_validation_check(
            required_fields=["endpoints", "database", "username", "password"],
            data={
                "endpoints": "10.0.0.1:5432",
                "database": "",
                "username": "user",
            },
        )

        # THEN
        assert not check.passed
        assert check.message == "Missing: database, password"
