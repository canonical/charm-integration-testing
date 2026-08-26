# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from validators.base import BaseValidator, ValidationLevel, ValidationResult
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import CharmBaseStub, RelationRoleStub, RelationStub
from validators.validate_action import run_validate_action


@dataclass
class ActionEventStub:
    """Minimal stand-in for ops.charm.ActionEvent, sufficient for exercising
    run_validate_action without pulling in a full ops.testing.Harness charm."""

    params: dict[str, Any] = field(default_factory=dict)
    results: dict[str, Any] | None = None
    failure_message: str | None = None

    def set_results(self, results: dict[str, Any]) -> None:
        self.results = results

    def fail(self, message: str = "") -> None:
        self.failure_message = message


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


class ErroringValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="ERROR",
            endpoint=self.endpoint,
            interface="postgresql_client",
            role=self.role,
            level=level,
            relation_id=self.relation_id,
            error="boom",
        )


def _make_charm() -> CharmBaseStub:
    relation = RelationStub(name="database", id=1)
    return make_charm_from_relation(relation, role=RelationRoleStub.requires, interface_name="postgresql_client")


class TestRunValidateAction:
    def test_defaults_to_simple_level(self) -> None:
        charm = _make_charm()
        event = ActionEventStub(params={})

        with patch(
            "validators.engine.engine.load_validators",
            return_value={"postgresql_client": [PassingValidator]},
        ):
            run_validate_action(charm, event)  # type: ignore[arg-type]

        assert event.results is not None
        payload = json.loads(event.results["results"])
        assert payload["results"][0]["level"] == "simple"
        assert event.failure_message is None

    def test_respects_requested_level(self) -> None:
        charm = _make_charm()
        event = ActionEventStub(params={"level": "deep"})

        with patch(
            "validators.engine.engine.load_validators",
            return_value={"postgresql_client": [PassingValidator]},
        ):
            run_validate_action(charm, event)  # type: ignore[arg-type]

        assert event.results is not None
        payload = json.loads(event.results["results"])
        assert payload["results"][0]["level"] == "deep"

    def test_rejects_invalid_level(self) -> None:
        charm = _make_charm()
        event = ActionEventStub(params={"level": "not-a-level"})

        run_validate_action(charm, event)  # type: ignore[arg-type]

        assert event.results is None
        assert event.failure_message is not None
        assert "not-a-level" in event.failure_message

    def test_fails_action_on_error_result(self) -> None:
        charm = _make_charm()
        event = ActionEventStub(params={})

        with patch(
            "validators.engine.engine.load_validators",
            return_value={"postgresql_client": [ErroringValidator]},
        ):
            run_validate_action(charm, event)  # type: ignore[arg-type]

        assert event.results is not None  # results are still reported
        assert event.failure_message is not None
        assert "boom" in event.failure_message

    def test_summary_counts_statuses(self) -> None:
        charm = _make_charm()
        event = ActionEventStub(params={})

        with patch(
            "validators.engine.engine.load_validators",
            return_value={"postgresql_client": [PassingValidator]},
        ):
            run_validate_action(charm, event)  # type: ignore[arg-type]

        assert event.results is not None
        assert event.results["summary"] == "pass=1 fail=0 error=0 skipped=0"
