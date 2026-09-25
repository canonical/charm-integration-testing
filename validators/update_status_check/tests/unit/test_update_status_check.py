# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from unittest.mock import patch

import ops
import ops.testing
import pytest

from validators.base import BaseValidator, ValidationLevel, ValidationResult, ValidationResultStatus
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import CharmBaseStub, RelationRoleStub, RelationStub
from validators.update_status_check import ValidationStatusStore, run_simple_check


def _make_charm() -> CharmBaseStub:
    relation = RelationStub(name="database", id=1)
    charm = make_charm_from_relation(relation, role=RelationRoleStub.requires, interface_name="postgresql_client")
    # Populate the remote databag so the engine doesn't skip this integration as
    # "not yet ready" (see validators.engine.engine._has_negotiated_data).
    integration = charm.model.relations["database"][0]
    integration.data[integration.app] = {"endpoints": "postgresql:5432"}
    return charm


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
        charm = _make_charm()

        with patch(
            "validators.engine.engine.load_validators",
            return_value={"postgresql_client": [PassingValidator]},
        ):
            results = run_simple_check(charm)  # type: ignore[arg-type]

        assert len(results.results) == 1
        assert results.results[0].status == "PASS"
        assert results.results[0].level == "simple"

    def test_logs_error_for_failing_result(self, caplog: pytest.LogCaptureFixture) -> None:
        charm = _make_charm()

        with (
            patch(
                "validators.engine.engine.load_validators",
                return_value={"postgresql_client": [FailingValidator]},
            ),
            caplog.at_level(logging.ERROR),
        ):
            results = run_simple_check(charm)  # type: ignore[arg-type]

        assert results.results[0].status == "FAIL"
        assert "database not reachable" in caplog.text
        assert "database" in caplog.text

    def test_no_error_logged_for_passing_result(self, caplog: pytest.LogCaptureFixture) -> None:
        charm = _make_charm()

        with (
            patch(
                "validators.engine.engine.load_validators",
                return_value={"postgresql_client": [PassingValidator]},
            ),
            caplog.at_level(logging.ERROR),
        ):
            run_simple_check(charm)  # type: ignore[arg-type]

        assert caplog.text == ""


class _MinimalCharm(ops.CharmBase):
    """Minimal concrete charm, sufficient to exercise `ValidationStatusStore`."""


def _validation_result(status: ValidationResultStatus, error: str | None = None) -> ValidationResult:
    return ValidationResult(
        status=status,
        endpoint="database",
        interface="postgresql_client",
        role="requires",
        level="simple",
        relation_id=1,
        error=error,
    )


class TestValidationStatusStore:
    def test_status_is_none_before_any_record(self) -> None:
        harness = ops.testing.Harness(_MinimalCharm)
        harness.begin()
        store = ValidationStatusStore(harness.charm)

        assert store.status() is None

        harness.cleanup()

    def test_record_with_failing_results_sets_blocked_status(self) -> None:
        harness = ops.testing.Harness(_MinimalCharm)
        harness.begin()
        store = ValidationStatusStore(harness.charm)

        store.record([_validation_result("FAIL")])

        status = store.status()
        assert isinstance(status, ops.BlockedStatus)
        assert "database" in status.message

        harness.cleanup()

    def test_record_with_only_passing_results_clears_status(self) -> None:
        harness = ops.testing.Harness(_MinimalCharm)
        harness.begin()
        store = ValidationStatusStore(harness.charm)
        store._stored.kind = "blocked"
        store._stored.message = "stale failure"

        store.record([_validation_result("PASS")])

        assert store.status() is None

        harness.cleanup()

    def test_record_with_skipped_results_preserves_prior_blocked_status(self) -> None:
        # A SKIPPED result means the check didn't re-run, so a prior failure stands.
        harness = ops.testing.Harness(_MinimalCharm)
        harness.begin()
        store = ValidationStatusStore(harness.charm)
        store._stored.kind = "blocked"
        store._stored.message = "stale failure"

        store.record([_validation_result("SKIPPED")])

        status = store.status()
        assert isinstance(status, ops.BlockedStatus)
        assert status.message == "stale failure"

        harness.cleanup()

    def test_record_with_no_results_preserves_prior_blocked_status(self) -> None:
        # Empty results can mean the check never ran (e.g. validator discovery failed).
        harness = ops.testing.Harness(_MinimalCharm)
        harness.begin()
        store = ValidationStatusStore(harness.charm)
        store._stored.kind = "blocked"
        store._stored.message = "stale failure"

        store.record([])

        status = store.status()
        assert isinstance(status, ops.BlockedStatus)
        assert status.message == "stale failure"

        harness.cleanup()

    def test_record_with_no_results_and_no_prior_status_stays_clear(self) -> None:
        harness = ops.testing.Harness(_MinimalCharm)
        harness.begin()
        store = ValidationStatusStore(harness.charm)

        store.record([])

        assert store.status() is None

        harness.cleanup()
