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
from typing import Optional
from unittest.mock import patch

from validators.base import BaseValidator, ValidationLevel, ValidationResult
from validators.runner.runner import ValidatorRunner, ValidatorRunnerResults

# ---------------------------------------------------------------------------
# Validator stubs
# ---------------------------------------------------------------------------


class PassingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="PASS",
            endpoint=self.endpoint,
            interface="test-interface",
            level=level,
            relation_id=self.relation_id,
        )


class FailingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return ValidationResult(
            status="FAIL",
            endpoint=self.endpoint,
            interface="test-interface",
            level=level,
            relation_id=self.relation_id,
        )


class ExplodingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        raise RuntimeError("something went wrong")


# ---------------------------------------------------------------------------
# Charm / entry-point stubs
# ---------------------------------------------------------------------------


@dataclass
class EndpointMetadataStub:
    interface_name: Optional[str]


@dataclass
class IntegrationStub:
    name: str
    id: int = 0


@dataclass
class ModelStub:
    relations: dict[str, list[IntegrationStub]]


@dataclass
class MetaStub:
    requires: dict[str, EndpointMetadataStub]


@dataclass
class CharmStub:
    meta: MetaStub
    model: ModelStub


@dataclass
class EntryPointStub:
    name: str
    _load_result: type = field(default=PassingValidator)
    _load_error: Optional[Exception] = field(default=None)

    def load(self) -> type:
        if self._load_error is not None:
            raise self._load_error
        return self._load_result


def _make_charm(requires: dict[str, Optional[str]], relations: dict[str, int]) -> CharmStub:
    """Build a CharmStub.

    Args:
        requires: mapping of endpoint name -> interface name (or None for fallback test).
        relations: mapping of endpoint name -> number of integrations.
    """
    return CharmStub(
        meta=MetaStub(requires={ep: EndpointMetadataStub(interface_name=iface) for ep, iface in requires.items()}),
        model=ModelStub(
            relations={ep: [IntegrationStub(name=ep, id=i) for i in range(n)] for ep, n in relations.items()}
        ),
    )


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
        charm = _make_charm(requires={"db": "test-interface"}, relations={"db": 1})

        # WHEN
        results = runner.run(charm, level="simple")  # type: ignore[arg-type]

        # THEN
        assert isinstance(results, ValidatorRunnerResults)
        assert len(results.results) == 1
        assert results.results[0].status == "PASS"

    def test_returns_fail_result(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", FailingValidator)
        charm = _make_charm(requires={"db": "test-interface"}, relations={"db": 1})

        # WHEN
        results = runner.run(charm, level="simple")  # type: ignore[arg-type]

        # THEN
        assert results.results[0].status == "FAIL"

    def test_captures_validator_exception_as_error(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", ExplodingValidator)
        charm = _make_charm(requires={"db": "test-interface"}, relations={"db": 1})

        # WHEN
        results = runner.run(charm, level="simple")  # type: ignore[arg-type]

        # THEN
        assert results.results[0].status == "ERROR"
        assert "something went wrong" in (results.results[0].error or "")

    def test_skips_interface_with_no_registered_validators(self) -> None:
        # GIVEN a runner with no validators for the endpoint's interface
        runner = ValidatorRunner.__new__(ValidatorRunner)
        runner.validators = {}
        charm = _make_charm(requires={"db": "test-interface"}, relations={"db": 1})

        # WHEN
        results = runner.run(charm, level="simple")  # type: ignore[arg-type]

        # THEN
        assert results.results == []

    def test_uses_endpoint_name_as_fallback_when_interface_is_none(self) -> None:
        # GIVEN an endpoint whose interface_name is None
        runner = self._runner_with("db", PassingValidator)
        charm = _make_charm(requires={"db": None}, relations={"db": 1})

        # WHEN
        results = runner.run(charm, level="simple")  # type: ignore[arg-type]

        # THEN
        assert results.results[0].status == "PASS"

    def test_passes_level_to_validator(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", PassingValidator)
        charm = _make_charm(requires={"db": "test-interface"}, relations={"db": 1})

        # WHEN
        results = runner.run(charm, level="deep")  # type: ignore[arg-type]

        # THEN
        assert results.results[0].level == "deep"

    def test_runs_across_multiple_integrations(self) -> None:
        # GIVEN two integrations on the same endpoint
        runner = self._runner_with("test-interface", PassingValidator)
        charm = _make_charm(requires={"db": "test-interface"}, relations={"db": 2})

        # WHEN
        results = runner.run(charm, level="simple")  # type: ignore[arg-type]

        # THEN
        assert len(results.results) == 2
        assert results.results[0].relation_id != results.results[1].relation_id
