# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from unittest.mock import patch

import pytest

from validators.base import BaseValidator, ValidationLevel, ValidationResult
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import RelationRoleStub, RelationStub
from validators.update_status_check import run_simple_check


class PassingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="PASS",
            endpoint=self.endpoint,
            interface="postgresql_client",
            role=self.role,
            level=level,
            relation_id=self.relation_id,
        )


class FailingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="FAIL",
            endpoint=self.endpoint,
            interface="postgresql_client",
            role=self.role,
            level=level,
            relation_id=self.relation_id,
            checks=[],
            error="database not reachable",
        )


class TestRunSimpleCheck:
    def test_runs_at_simple_level_and_returns_results(self) -> None:
        relation = RelationStub(name="database", id=1)
        charm = make_charm_from_relation(relation, role=RelationRoleStub.requires, interface_name="postgresql_client")

        with patch(
            "validators.update_status_check.update_status_check._load_validators",
            return_value={"postgresql_client": [PassingValidator]},
        ):
            results = run_simple_check(charm)  # type: ignore[arg-type]

        assert len(results.results) == 1
        assert results.results[0].status == "PASS"
        assert results.results[0].level == "simple"

    def test_logs_error_for_failing_result(self, caplog: pytest.LogCaptureFixture) -> None:
        relation = RelationStub(name="database", id=1)
        charm = make_charm_from_relation(relation, role=RelationRoleStub.requires, interface_name="postgresql_client")

        with (
            patch(
                "validators.update_status_check.update_status_check._load_validators",
                return_value={"postgresql_client": [FailingValidator]},
            ),
            caplog.at_level(logging.ERROR),
        ):
            results = run_simple_check(charm)  # type: ignore[arg-type]

        assert results.results[0].status == "FAIL"
        assert "database not reachable" in caplog.text
        assert "database" in caplog.text

    def test_no_error_logged_for_passing_result(self, caplog: pytest.LogCaptureFixture) -> None:
        relation = RelationStub(name="database", id=1)
        charm = make_charm_from_relation(relation, role=RelationRoleStub.requires, interface_name="postgresql_client")

        with (
            patch(
                "validators.update_status_check.update_status_check._load_validators",
                return_value={"postgresql_client": [PassingValidator]},
            ),
            caplog.at_level(logging.ERROR),
        ):
            run_simple_check(charm)  # type: ignore[arg-type]

        assert caplog.text == ""
