# Copyright (C) 2025 Canonical Ltd
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

from importlib.metadata import EntryPoint
from typing import Any
from unittest.mock import MagicMock, patch

from validators.base import BaseValidator, ValidationLevel, ValidationResult
from validators.runner.runner import ValidatorRunner, ValidatorRunnerResults


class PassingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="PASS",
            endpoint=self.endpoint,
            interface="test-interface",
            level=level,
        )


class FailingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="FAIL",
            endpoint=self.endpoint,
            interface="test-interface",
            level=level,
        )


class ExplodingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        raise RuntimeError("something went wrong")


def _make_charm_stub(requires: dict[str, str], relations: dict[str, list[Any]]) -> MagicMock:
    """Build a minimal CharmBase stub with requires metadata and model relations."""
    charm = MagicMock()
    charm.meta.requires = {endpoint: MagicMock(interface_name=interface) for endpoint, interface in requires.items()}
    charm.model.relations = {}
    for endpoint, integrations in relations.items():
        mocks = []
        for _ in integrations:
            m = MagicMock()
            m.name = endpoint
            mocks.append(m)
        charm.model.relations[endpoint] = mocks
    return charm


class TestValidatorRunnerLoadValidators:
    def test_skips_non_base_validator_entry_points(self) -> None:
        # GIVEN an entry point that loads a class not implementing BaseValidator
        class NotAValidator:
            pass

        ep = MagicMock(spec=EntryPoint)
        ep.name = "test-interface"
        ep.load.return_value = NotAValidator

        with patch("validators.runner.runner.entry_points", return_value=[ep]):
            # WHEN
            validators = ValidatorRunner._load_validators()

        # THEN
        assert validators == {}

    def test_skips_entry_points_that_fail_to_load(self) -> None:
        # GIVEN an entry point that raises on load
        ep = MagicMock(spec=EntryPoint)
        ep.name = "test-interface"
        ep.load.side_effect = ImportError("missing dep")

        with patch("validators.runner.runner.entry_points", return_value=[ep]):
            # WHEN
            validators = ValidatorRunner._load_validators()

        # THEN
        assert validators == {}

    def test_loads_valid_validator(self) -> None:
        # GIVEN a well-formed entry point
        ep = MagicMock(spec=EntryPoint)
        ep.name = "test-interface"
        ep.load.return_value = PassingValidator

        with patch("validators.runner.runner.entry_points", return_value=[ep]):
            # WHEN
            validators = ValidatorRunner._load_validators()

        # THEN
        assert "test-interface" in validators
        assert PassingValidator in validators["test-interface"]

    def test_groups_multiple_validators_under_same_interface(self) -> None:
        # GIVEN two entry points for the same interface name
        ep1 = MagicMock(spec=EntryPoint)
        ep1.name = "test-interface"
        ep1.load.return_value = PassingValidator

        ep2 = MagicMock(spec=EntryPoint)
        ep2.name = "test-interface"
        ep2.load.return_value = FailingValidator

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
        charm = _make_charm_stub(
            requires={"db": "test-interface"},
            relations={"db": [MagicMock()]},
        )

        # WHEN
        results = runner.run(charm, level="simple")

        # THEN
        assert isinstance(results, ValidatorRunnerResults)
        assert len(results.results) == 1
        assert results.results[0].status == "PASS"

    def test_returns_fail_result(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", FailingValidator)
        charm = _make_charm_stub(
            requires={"db": "test-interface"},
            relations={"db": [MagicMock()]},
        )

        # WHEN
        results = runner.run(charm, level="simple")

        # THEN
        assert results.results[0].status == "FAIL"

    def test_captures_validator_exception_as_error(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", ExplodingValidator)
        charm = _make_charm_stub(
            requires={"db": "test-interface"},
            relations={"db": [MagicMock()]},
        )

        # WHEN
        results = runner.run(charm, level="simple")

        # THEN
        assert results.results[0].status == "ERROR"
        assert "something went wrong" in (results.results[0].error or "")

    def test_skips_interface_with_no_registered_validators(self) -> None:
        # GIVEN a runner with no validators for the endpoint's interface
        runner = ValidatorRunner.__new__(ValidatorRunner)
        runner.validators = {}
        charm = _make_charm_stub(
            requires={"db": "test-interface"},
            relations={"db": [MagicMock()]},
        )

        # WHEN
        results = runner.run(charm, level="simple")

        # THEN
        assert results.results == []

    def test_uses_endpoint_name_as_fallback_when_interface_is_none(self) -> None:
        # GIVEN an endpoint whose interface_name is None
        runner = self._runner_with("db", PassingValidator)
        integration = MagicMock()
        integration.name = "db"
        charm = MagicMock()
        charm.meta.requires = {"db": MagicMock(interface_name=None)}
        charm.model.relations = {"db": [integration]}

        # WHEN
        results = runner.run(charm, level="simple")

        # THEN
        assert results.results[0].status == "PASS"

    def test_passes_level_to_validator(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", PassingValidator)
        charm = _make_charm_stub(
            requires={"db": "test-interface"},
            relations={"db": [MagicMock()]},
        )

        # WHEN
        results = runner.run(charm, level="deep")

        # THEN
        assert results.results[0].level == "deep"

    def test_runs_across_multiple_integrations(self) -> None:
        # GIVEN two integrations on the same endpoint
        runner = self._runner_with("test-interface", PassingValidator)
        charm = _make_charm_stub(
            requires={"db": "test-interface"},
            relations={"db": [MagicMock(), MagicMock()]},
        )

        # WHEN
        results = runner.run(charm, level="simple")

        # THEN
        assert len(results.results) == 2
