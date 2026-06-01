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

from dataclasses import dataclass, field
from typing import Optional, cast
from unittest.mock import patch

import ops
import pytest

from validators.base import BaseValidator, ValidationLevel, ValidationResult  # type: ignore
from validators.runner.runner import ValidatorRunner, ValidatorRunnerResults  # type: ignore[import-not-found]
from validators.test_utils.helpers import make_charm_from_relation  # type: ignore[import-not-found]
from validators.test_utils.stubs import (  # type: ignore[import-not-found]
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Validator stubs
# ---------------------------------------------------------------------------


class PassingValidator(BaseValidator):  # type: ignore[misc]
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="PASS",
            endpoint=self.endpoint,
            interface="test-interface",
            role=self.role,
            level=level,
            relation_id=self.relation_id,
        )


class FailingValidator(BaseValidator):  # type: ignore[misc]
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="FAIL",
            endpoint=self.endpoint,
            interface="test-interface",
            role=self.role,
            level=level,
            relation_id=self.relation_id,
        )


class ExplodingValidator(BaseValidator):  # type: ignore[misc]
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        raise RuntimeError("something went wrong")


class SkippingValidator(BaseValidator):  # type: ignore[misc]
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


# ---------------------------------------------------------------------------
# Charm / entry-point stubs
# ---------------------------------------------------------------------------


@dataclass
class EntryPointStub:
    name: str
    _load_result: type = field(default=PassingValidator)
    _load_error: Optional[Exception] = field(default=None)

    def load(self) -> type:
        if self._load_error is not None:
            raise self._load_error
        return self._load_result


class TestValidatorRunnerLoadValidators:
    def test_skips_non_base_validator_entry_points(self) -> None:
        # GIVEN an entry point that loads a class not implementing BaseValidator
        class NotAValidator:
            pass

        entry_point = EntryPointStub(name="test-interface", _load_result=NotAValidator)

        with patch("validators.runner.runner.entry_points", return_value=[entry_point]):
            # WHEN
            validators = ValidatorRunner._load_validators()

        # THEN
        assert validators == {}

    def test_skips_entry_points_that_fail_to_load(self) -> None:
        # GIVEN an entry point that raises on load
        entry_point = EntryPointStub(name="test-interface", _load_error=ImportError("missing dep"))

        with patch("validators.runner.runner.entry_points", return_value=[entry_point]):
            # WHEN
            validators = ValidatorRunner._load_validators()

        # THEN
        assert validators == {}

    def test_loads_valid_validator(self) -> None:
        # GIVEN a well-formed entry point
        entry_point = EntryPointStub(name="test-interface", _load_result=PassingValidator)

        with patch("validators.runner.runner.entry_points", return_value=[entry_point]):
            # WHEN
            validators = ValidatorRunner._load_validators()

        # THEN
        assert "test-interface" in validators
        assert PassingValidator in validators["test-interface"]

    def test_groups_multiple_validators_under_same_interface(self) -> None:
        # GIVEN two entry points for the same interface name
        ep1 = EntryPointStub(name="test-interface", _load_result=PassingValidator)
        ep2 = EntryPointStub(name="test-interface", _load_result=FailingValidator)

        with patch("validators.runner.runner.entry_points", return_value=[ep1, ep2]):
            # WHEN
            validators = ValidatorRunner._load_validators()

        # THEN
        assert len(validators["test-interface"]) == 2


class TestValidatorRunnerRun:
    def _runner_with(self, interface: str, validator_cls: type[BaseValidator]) -> ValidatorRunner:
        runner = ValidatorRunner.__new__(ValidatorRunner)
        runner.validators = {interface: [validator_cls]}
        return runner

    def test_returns_pass_result(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", PassingValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.run(cast(ops.CharmBase, charm), level="simple")

        # THEN
        assert isinstance(results, ValidatorRunnerResults)
        assert len(results.results) == 1
        assert results.results[0].status == "PASS"

    def test_returns_fail_result(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", FailingValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.run(cast(ops.CharmBase, charm), level="simple")

        # THEN
        assert results.results[0].status == "FAIL"

    def test_captures_validator_exception_as_error(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", ExplodingValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.run(cast(ops.CharmBase, charm), level="simple")

        # THEN
        assert results.results[0].status == "ERROR"
        assert "something went wrong" in (results.results[0].error or "")

    def test_skips_interface_with_no_registered_validators(self) -> None:
        # GIVEN a runner with no validators for the endpoint's interface
        runner = ValidatorRunner.__new__(ValidatorRunner)
        runner.validators = {}
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.run(cast(ops.CharmBase, charm), level="simple")

        # THEN
        assert results.results == []

    def test_uses_endpoint_name_as_fallback_when_interface_is_none(self) -> None:
        # GIVEN an endpoint whose interface_name is None
        runner = self._runner_with("db", PassingValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, role=RelationRoleStub.requires, interface_name=None)

        # WHEN
        results = runner.run(cast(ops.CharmBase, charm), level="simple")

        # THEN
        assert results.results[0].status == "PASS"

    def test_passes_level_to_validator(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", PassingValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.run(cast(ops.CharmBase, charm), level="deep")

        # THEN
        assert results.results[0].level == "deep"

    def test_runs_across_multiple_integrations(self) -> None:
        # GIVEN two integrations on the same endpoint
        runner = self._runner_with("test-interface", PassingValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(
            relation, interface_name="test-interface", role=RelationRoleStub.requires, integrations_count=2
        )

        # WHEN
        results = runner.run(cast(ops.CharmBase, charm), level="simple")

        # THEN
        assert len(results.results) == 2

    def test_falls_back_to_simple_when_deep_is_skipped(self) -> None:
        # GIVEN a validator that only supports simple
        runner = self._runner_with("test-interface", SkippingValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN the runner is asked for deep
        results = runner.run(cast(ops.CharmBase, charm), level="deep")

        # THEN it fell back and got a real result at simple
        assert results.results[0].status == "PASS"
        assert results.results[0].level == "simple"

    def test_falls_back_through_two_levels(self) -> None:
        # GIVEN a validator that only supports simple, but uat is requested
        runner = self._runner_with("test-interface", SkippingValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN the runner is asked for uat
        results = runner.run(cast(ops.CharmBase, charm), level="uat")

        # THEN it fell back through deep → simple and got a real result
        assert results.results[0].status == "PASS"
        assert results.results[0].level == "simple"

    def test_surfaces_skipped_when_even_simple_is_not_supported(self) -> None:
        # GIVEN a validator that skips every level
        class AlwaysSkippingValidator(BaseValidator):  # type: ignore[misc]
            def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
                return self._skipped_result_due_to_level(level)

        runner = self._runner_with("test-interface", AlwaysSkippingValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(
            relation, interface_name="test-interface", role=RelationRoleStub.requires, integrations_count=2
        )

        # WHEN
        results = runner.run(cast(ops.CharmBase, charm), level="simple")

        # THEN both integrations produce independent SKIPPED results
        assert results.results[0].status == "SKIPPED"
        assert results.results[1].status == "SKIPPED"
        assert results.results[0].relation_id != results.results[1].relation_id

    @pytest.mark.parametrize(
        "role,ignore_validator",
        [(RelationRoleStub.peer, True), (RelationRoleStub.provides, False), (RelationRoleStub.requires, False)],
    )
    def test_skips_based_on_relation(self, role: RelationRoleStub, ignore_validator: bool) -> None:
        # GIVEN a runner with a validator registered for the interface with the given role
        runner = self._runner_with("test-interface", PassingValidator)
        relation = RelationStub(name="cluster", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=role)

        # WHEN
        results = runner.run(cast(ops.CharmBase, charm), level="simple")

        if ignore_validator:
            # THEN the validator is ignored
            assert results.results == []
        else:
            # THEN the relation is processed and results are returned
            assert len(results.results) == 1
            assert results.results[0].status == "PASS"
            assert results.results[0].role == role.value
