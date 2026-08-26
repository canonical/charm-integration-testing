# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from typing import Optional, cast

import ops
import pytest

from validators.base import BaseValidator, ValidationLevel, ValidationResult
from validators.engine.engine import load_validators, run_for_charm
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import RelationRoleStub, RelationStub


class PassingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="PASS",
            endpoint=self.endpoint,
            interface="test-interface",
            role=self.role,
            level=level,
            relation_id=self.relation_id,
        )


class FailingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="FAIL",
            endpoint=self.endpoint,
            interface="test-interface",
            role=self.role,
            level=level,
            relation_id=self.relation_id,
        )


class ExplodingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        raise RuntimeError("something went wrong")


class SkippingValidator(BaseValidator):
    """Validator that only supports 'simple'; returns SKIPPED for anything else."""

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if level != "simple":
            return self._skipped_result_due_to_level(level)
        return ValidationResult(
            status="PASS",
            endpoint=self.endpoint,
            interface="test-interface",
            role=self.role,
            level=level,
            relation_id=self.relation_id,
        )


@dataclass
class EntryPointStub:
    name: str
    _load_result: type = field(default=PassingValidator)
    _load_error: Optional[Exception] = field(default=None)

    def load(self) -> type:
        if self._load_error is not None:
            raise self._load_error
        return self._load_result


class TestLoadValidators:
    def test_skips_non_base_validator_entry_points(self) -> None:
        class NotAValidator:
            pass

        entry_point = EntryPointStub(name="test-interface", _load_result=NotAValidator)

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("validators.engine.engine.entry_points", lambda group: [entry_point])
            validators = load_validators()

        assert validators == {}

    def test_skips_entry_points_that_fail_to_load(self, caplog: pytest.LogCaptureFixture) -> None:
        entry_point = EntryPointStub(name="test-interface", _load_error=ImportError("missing dep"))

        with pytest.MonkeyPatch().context() as mp, caplog.at_level("ERROR"):
            mp.setattr("validators.engine.engine.entry_points", lambda group: [entry_point])
            validators = load_validators()

        assert validators == {}
        assert "ImportError: missing dep" in caplog.text

    def test_groups_multiple_validators_under_same_interface(self) -> None:
        ep1 = EntryPointStub(name="test-interface", _load_result=PassingValidator)
        ep2 = EntryPointStub(name="test-interface", _load_result=FailingValidator)

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("validators.engine.engine.entry_points", lambda group: [ep1, ep2])
            validators = load_validators()

        assert len(validators["test-interface"]) == 2


class TestRunForCharm:
    def test_returns_pass_result(self) -> None:
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        results = run_for_charm(
            cast(ops.CharmBase, charm), level="simple", validators={"test-interface": [PassingValidator]}
        )

        assert len(results) == 1
        assert results[0].status == "PASS"

    def test_returns_fail_result(self) -> None:
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        results = run_for_charm(
            cast(ops.CharmBase, charm), level="simple", validators={"test-interface": [FailingValidator]}
        )

        assert results[0].status == "FAIL"

    def test_captures_validator_exception_as_error(self) -> None:
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        results = run_for_charm(
            cast(ops.CharmBase, charm), level="simple", validators={"test-interface": [ExplodingValidator]}
        )

        assert results[0].status == "ERROR"
        assert "something went wrong" in (results[0].error or "")

    def test_reports_error_when_relation_missing_from_model(self) -> None:
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)
        charm.model.relations = {}

        results = run_for_charm(
            cast(ops.CharmBase, charm), level="simple", validators={"test-interface": [PassingValidator]}
        )

        assert results[0].status == "ERROR"
        assert results[0].relation_id is None

    def test_skips_peer_relations(self) -> None:
        relation = RelationStub(name="cluster", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.peer)

        results = run_for_charm(
            cast(ops.CharmBase, charm), level="simple", validators={"test-interface": [PassingValidator]}
        )

        assert results == []

    def test_falls_back_through_two_levels(self) -> None:
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        results = run_for_charm(
            cast(ops.CharmBase, charm), level="uat", validators={"test-interface": [SkippingValidator]}
        )

        assert results[0].status == "PASS"
        assert results[0].level == "simple"

    def test_runs_across_multiple_integrations(self) -> None:
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(
            relation, interface_name="test-interface", role=RelationRoleStub.requires, integrations_count=2
        )

        results = run_for_charm(
            cast(ops.CharmBase, charm), level="simple", validators={"test-interface": [PassingValidator]}
        )

        assert len(results) == 2

    def test_loads_validators_when_none_supplied(self) -> None:
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("validators.engine.engine.load_validators", lambda: {"test-interface": [PassingValidator]})
            results = run_for_charm(cast(ops.CharmBase, charm), level="simple")

        assert results[0].status == "PASS"
