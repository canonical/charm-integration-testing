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

import pytest

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult


class ConcreteValidator(BaseValidator):
    """Minimal concrete implementation for testing BaseValidator."""

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="PASS",
            endpoint=self.endpoint,
            interface="test-interface",
            level=level,
        )


class TestValidationCheck:
    def test_passed(self) -> None:
        # GIVEN / WHEN
        check = ValidationCheck(name="connect", passed=True, message="OK")

        # THEN
        assert check.name == "connect"
        assert check.passed is True
        assert check.message == "OK"

    def test_message_defaults_to_empty_string(self) -> None:
        # GIVEN / WHEN
        check = ValidationCheck(name="schema", passed=False)

        # THEN
        assert check.message == ""


class TestValidationResult:
    def test_pass_result(self) -> None:
        # GIVEN / WHEN
        result = ValidationResult(status="PASS", endpoint="db", interface="postgresql_client", level="simple")

        # THEN
        assert result.status == "PASS"
        assert result.checks == []
        assert result.error is None

    def test_fail_result_with_checks(self) -> None:
        # GIVEN
        checks = [
            ValidationCheck(name="schema", passed=True),
            ValidationCheck(name="connect", passed=False, message="Connection refused"),
        ]

        # WHEN
        result = ValidationResult(
            status="FAIL",
            endpoint="db",
            interface="postgresql_client",
            level="simple",
            checks=checks,
        )

        # THEN
        assert result.status == "FAIL"
        assert len(result.checks) == 2
        assert result.checks[1].message == "Connection refused"

    def test_error_result(self) -> None:
        # GIVEN / WHEN
        result = ValidationResult(
            status="ERROR",
            endpoint="db",
            interface="postgresql_client",
            level="simple",
            error="Unexpected exception",
        )

        # THEN
        assert result.status == "ERROR"
        assert result.error == "Unexpected exception"

    def test_serialises_to_json(self) -> None:
        # GIVEN
        result = ValidationResult(
            status="PASS",
            endpoint="db",
            interface="postgresql_client",
            level="simple",
        )

        # WHEN
        json_str = result.model_dump_json()

        # THEN
        assert '"status":"PASS"' in json_str
        assert '"interface":"postgresql_client"' in json_str


class TestBaseValidator:
    def test_stores_charm_and_endpoint(self) -> None:
        # GIVEN
        charm = object()

        # WHEN
        validator = ConcreteValidator(charm, "my-db")  # type: ignore[arg-type]

        # THEN
        assert validator.charm is charm
        assert validator.endpoint == "my-db"

    def test_validate_returns_result(self) -> None:
        # GIVEN
        validator = ConcreteValidator(object(), "my-db")  # type: ignore[arg-type]

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert isinstance(result, ValidationResult)
        assert result.status == "PASS"
        assert result.level == "simple"

    def test_cannot_instantiate_abstract_class(self) -> None:
        # GIVEN / WHEN / THEN
        with pytest.raises(TypeError):
            BaseValidator(object(), "endpoint")  # type: ignore[abstract, arg-type]
