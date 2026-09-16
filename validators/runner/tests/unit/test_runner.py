# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, cast
from unittest.mock import patch

import ops
import pytest

from validators.base import (
    BasePersistenceValidator,
    BaseValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)
from validators.runner import runner
from validators.runner.runner import ValidatorRunner, ValidatorRunnerResults, _parse_cli_args, logger
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    CharmBaseStub,
    CharmMetaStub,
    ModelStub,
    RelationMetaStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Validator stubs
# ---------------------------------------------------------------------------


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


class PreparingPersistenceValidator(BasePersistenceValidator):
    """Persistence validator that succeeds at every lifecycle stage."""

    cleanup_calls: list[int] = []

    def prepare(self) -> PersistenceState:
        return PersistenceState(id=self.relation_id + 100, ref=1)

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        check = ValidationCheck(name="row_count", passed=True, message="OK")
        result = self._make_result(status="PASS", level="deep", interface="test-interface", checks=[check])
        return result, PersistenceState(id=expected.id, ref=expected.ref + 1)

    def cleanup(self) -> None:
        PreparingPersistenceValidator.cleanup_calls.append(self.relation_id)


class ExplodingPersistenceValidator(BasePersistenceValidator):
    """Persistence validator that raises on every lifecycle stage."""

    def prepare(self) -> PersistenceState:
        raise RuntimeError("prepare exploded")

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        raise RuntimeError("checkpoint exploded")

    def cleanup(self) -> None:
        raise RuntimeError("cleanup exploded")


class FailingPersistenceValidator(BasePersistenceValidator):
    """Persistence validator whose checkpoint() fails but still returns an advanced state.

    Mirrors the reference PostgreSQL persistence validator, which increments and returns the ref
    alongside a failed row-count check.
    """

    def prepare(self) -> PersistenceState:
        return PersistenceState(id=self.relation_id + 100, ref=1)

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        check = ValidationCheck(name="row_count", passed=False, message="mismatch")
        result = self._make_result(status="FAIL", level="deep", interface="test-interface", checks=[check])
        return result, PersistenceState(id=expected.id, ref=expected.ref + 1)

    def cleanup(self) -> None:
        pass


class NotApplicablePersistenceValidator(BasePersistenceValidator):
    """Persistence validator that is never applicable (e.g. wrong relation side)."""

    def prepare(self) -> PersistenceState:
        raise PersistenceNotApplicable("not applicable")

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        raise PersistenceNotApplicable("not applicable")

    def cleanup(self) -> None:
        raise PersistenceNotApplicable("not applicable")


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

    def test_skips_entry_points_that_fail_to_load(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN an entry point that raises on load
        entry_point = EntryPointStub(name="test-interface", _load_error=ImportError("missing dep"))

        with caplog.at_level(logging.ERROR, logger="validators"):
            with patch("validators.runner.runner.entry_points", return_value=[entry_point]):
                # WHEN
                validators = ValidatorRunner._load_validators()

        # THEN validators are skipped and the full traceback is captured, not just the message
        assert validators == {}
        assert "Traceback" in caplog.text
        assert "ImportError: missing dep" in caplog.text

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


class TestValidatorRunnerLoadPersistenceValidators:
    def _runner(self) -> ValidatorRunner:
        runner = ValidatorRunner.__new__(ValidatorRunner)
        runner.persistence_load_errors = {}
        return runner

    def test_loads_valid_persistence_validator(self) -> None:
        # GIVEN a well-formed persistence entry point
        entry_point = EntryPointStub(name="test-interface", _load_result=PreparingPersistenceValidator)

        with patch("validators.runner.runner.entry_points", return_value=[entry_point]):
            # WHEN
            validators = self._runner()._load_persistence_validators()

        # THEN
        assert validators["test-interface"] == [PreparingPersistenceValidator]

    def test_skips_non_base_persistence_validator_entry_points(self) -> None:
        # GIVEN an entry point that loads a class not implementing BasePersistenceValidator
        class NotAPersistenceValidator:
            pass

        entry_point = EntryPointStub(name="test-interface", _load_result=NotAPersistenceValidator)

        with patch("validators.runner.runner.entry_points", return_value=[entry_point]):
            # WHEN
            validators = self._runner()._load_persistence_validators()

        # THEN
        assert validators == {}

    def test_warns_when_multiple_persistence_validators_share_an_interface(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # GIVEN two persistence entry points registered under the same interface name
        ep1 = EntryPointStub(name="test-interface", _load_result=PreparingPersistenceValidator)
        ep2 = EntryPointStub(name="test-interface", _load_result=ExplodingPersistenceValidator)

        with caplog.at_level(logging.WARNING, logger="validators"):
            with patch("validators.runner.runner.entry_points", return_value=[ep1, ep2]):
                # WHEN
                validators = self._runner()._load_persistence_validators()

        # THEN both are still registered, but a warning explains the state-overwrite risk
        assert len(validators["test-interface"]) == 2
        assert "Multiple persistence validators registered for interface 'test-interface'" in caplog.text

    def test_records_load_error_for_interface_when_entry_point_load_raises(self) -> None:
        # GIVEN an entry point whose load() raises (e.g. a broken/incompatible package)
        entry_point = EntryPointStub(name="test-interface", _load_error=RuntimeError("broken import"))

        with patch("validators.runner.runner.entry_points", return_value=[entry_point]):
            # WHEN
            runner = self._runner()
            validators = runner._load_persistence_validators()

        # THEN the interface has no registered validator, but the failure is recorded so
        # prepare_all/checkpoint_all/cleanup_all can surface it instead of silently treating a
        # relation on this interface as having no applicable persistence validator
        assert validators == {}
        assert "broken import" in runner.persistence_load_errors["test-interface"]


class TestParseCliArgs:
    def test_defaults_to_simple_level_with_no_flags(self) -> None:
        # WHEN no flags are passed at all
        args, refs = _parse_cli_args([])

        # THEN the pre-persistence CLI contract is preserved: level defaults to "simple"
        assert args.level == "simple"
        assert args.persistence is None
        assert refs == {}

    def test_persistence_only_invocation_leaves_level_unset(self) -> None:
        # WHEN only --persistence is passed
        args, refs = _parse_cli_args(["--persistence", "prepare"])

        # THEN level stays None rather than defaulting, so main() skips the functional run
        assert args.level is None
        assert args.persistence == "prepare"

    def test_level_and_persistence_can_be_combined(self) -> None:
        # WHEN both --level and --persistence are passed
        args, refs = _parse_cli_args(["--level", "deep", "--persistence", "cleanup"])

        # THEN both are honoured as given, with no default substitution
        assert args.level == "deep"
        assert args.persistence == "cleanup"

    def test_checkpoint_requires_refs(self) -> None:
        # WHEN --persistence checkpoint is passed without --refs
        with pytest.raises(SystemExit):
            # THEN argparse's parser.error() exits the process
            _parse_cli_args(["--persistence", "checkpoint"])

    def test_checkpoint_parses_refs_json_into_persistence_state(self) -> None:
        # WHEN --refs is a valid JSON dict of relation_id -> PersistenceState
        refs_json = json.dumps({"4": {"id": 1, "ref": 2}})

        args, refs = _parse_cli_args(["--persistence", "checkpoint", "--refs", refs_json])

        # THEN it's decoded into PersistenceState objects keyed by relation_id string
        assert refs == {"4": PersistenceState(id=1, ref=2)}

    def test_invalid_refs_json_exits(self) -> None:
        # WHEN --refs is not valid JSON
        with pytest.raises(SystemExit):
            _parse_cli_args(["--persistence", "checkpoint", "--refs", "not-json"])

    def test_refs_not_matching_persistence_state_schema_exits(self) -> None:
        # WHEN --refs is valid JSON but doesn't match PersistenceState's schema
        refs_json = json.dumps({"4": {"unexpected": "shape"}})

        with pytest.raises(SystemExit):
            _parse_cli_args(["--persistence", "checkpoint", "--refs", refs_json])

    def test_refs_rejected_when_persistence_is_not_checkpoint(self) -> None:
        # Regression test for: --refs was silently ignored when combined with --persistence
        # prepare/cleanup or with no --persistence at all, so a typo like
        # "--persistence prepare --refs ..." would run a different lifecycle op than the
        # supplied --refs implied, instead of failing loudly.
        refs_json = json.dumps({"4": {"id": 1, "ref": 2}})

        with pytest.raises(SystemExit):
            _parse_cli_args(["--persistence", "prepare", "--refs", refs_json])

    def test_refs_rejected_when_persistence_is_cleanup(self) -> None:
        refs_json = json.dumps({"4": {"id": 1, "ref": 2}})

        with pytest.raises(SystemExit):
            _parse_cli_args(["--persistence", "cleanup", "--refs", refs_json])


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

    def test_returns_pass_result_with_provides(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", PassingValidator)
        relation = RelationStub(name="api", id=0)
        charm = make_charm_from_relation(relation, role=RelationRoleStub.provides, interface_name="test-interface")

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
        class AlwaysSkippingValidator(BaseValidator):
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


class TestValidatorRunnerPersistence:
    def _runner_with(self, interface: str, validator_cls: type[BasePersistenceValidator]) -> ValidatorRunner:
        runner = ValidatorRunner.__new__(ValidatorRunner)
        runner.validators = {}
        runner.persistence_validators = {interface: [validator_cls]}
        runner.persistence_load_errors = {}
        return runner

    def setup_method(self) -> None:
        PreparingPersistenceValidator.cleanup_calls = []

    def test_prepare_all_seeds_state_keyed_by_relation_id(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", PreparingPersistenceValidator)
        relation = RelationStub(name="db", id=5)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.prepare_all(cast(ops.CharmBase, charm))

        # THEN
        assert results.results == []
        assert "5" in results.updated_refs
        assert results.updated_refs["5"].ref == 1

    def test_prepare_all_ignores_peer_relations(self) -> None:
        # GIVEN a peer relation whose interface has a registered persistence validator
        runner = self._runner_with("test-interface", PreparingPersistenceValidator)
        relation = RelationStub(name="cluster", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.peer)

        # WHEN
        results = runner.prepare_all(cast(ops.CharmBase, charm))

        # THEN
        assert results.updated_refs == {}

    def test_prepare_all_skips_not_applicable_validators(self) -> None:
        # GIVEN a validator that is never applicable (e.g. wrong relation side)
        runner = self._runner_with("test-interface", NotApplicablePersistenceValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.prepare_all(cast(ops.CharmBase, charm))

        # THEN it's skipped silently, not reported as an error
        assert results.results == []
        assert results.updated_refs == {}

    def test_prepare_all_captures_exception_as_error_result(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", ExplodingPersistenceValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.prepare_all(cast(ops.CharmBase, charm))

        # THEN
        assert len(results.results) == 1
        assert results.results[0].status == "ERROR"
        assert "prepare exploded" in (results.results[0].error or "")
        assert results.updated_refs == {}

    def test_checkpoint_all_verifies_and_advances_state(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", PreparingPersistenceValidator)
        relation = RelationStub(name="db", id=5)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)
        refs = {"5": PersistenceState(id=105, ref=1)}

        # WHEN
        results = runner.checkpoint_all(cast(ops.CharmBase, charm), refs)

        # THEN
        assert len(results.results) == 1
        assert results.results[0].status == "PASS"
        assert results.updated_refs["5"].ref == 2
        assert results.updated_refs["5"].id == 105

    def test_checkpoint_all_does_not_advance_state_on_fail(self) -> None:
        # Regression test for: checkpoint_all() previously recorded the new state returned
        # alongside a FAIL result unconditionally, overwriting the last-known-good baseline before
        # JujuClient raises on the failure. A retry/continued run would then checkpoint against the
        # post-failure state instead of the original baseline, and could spuriously pass.
        runner = self._runner_with("test-interface", FailingPersistenceValidator)
        relation = RelationStub(name="db", id=5)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)
        refs = {"5": PersistenceState(id=105, ref=1)}

        # WHEN
        results = runner.checkpoint_all(cast(ops.CharmBase, charm), refs)

        # THEN the FAIL result is still reported, but the previous baseline is preserved rather
        # than being overwritten with the failed checkpoint's advanced state.
        assert len(results.results) == 1
        assert results.results[0].status == "FAIL"
        assert results.updated_refs == {}

    def test_checkpoint_all_reports_error_for_missing_relation(self) -> None:
        # GIVEN a ref pointing at a relation_id no longer present in the model
        runner = self._runner_with("test-interface", PreparingPersistenceValidator)
        relation = RelationStub(name="db", id=5)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)
        refs = {"999": PersistenceState(id=1, ref=1)}

        # WHEN
        results = runner.checkpoint_all(cast(ops.CharmBase, charm), refs)

        # THEN
        assert len(results.results) == 1
        assert results.results[0].status == "ERROR"
        assert results.updated_refs == {}

    def test_checkpoint_all_reports_error_for_non_integer_relation_ids(self) -> None:
        # GIVEN a malformed ref key
        runner = self._runner_with("test-interface", PreparingPersistenceValidator)
        relation = RelationStub(name="db", id=5)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)
        refs = {"not-an-int": PersistenceState(id=1, ref=1)}

        # WHEN
        results = runner.checkpoint_all(cast(ops.CharmBase, charm), refs)

        # THEN no crash, and the malformed entry is reported as an ERROR rather than silently
        # discarded, so a real durability check can't pass without ever running
        assert len(results.results) == 1
        assert results.results[0].status == "ERROR"
        assert "Invalid relation_id" in (results.results[0].error or "")
        assert results.updated_refs == {}

    def test_checkpoint_all_does_not_resolve_a_ref_to_a_colliding_peer_relation(self) -> None:
        # Regression test for: _find_relation_by_id() (used by checkpoint_all) previously did not
        # skip peer relations the way _iter_persistence_targets()/_persistence_load_error_results()
        # do, so a stale or malformed --refs entry whose relation_id happened to collide with a
        # peer relation's id could resolve to that peer relation instead of failing loudly.
        runner = self._runner_with("test-interface", PreparingPersistenceValidator)
        peer_relation = RelationStub(name="cluster", id=5, app=ApplicationStub(name="app"))
        charm = CharmBaseStub(
            meta=CharmMetaStub(
                relations={
                    "cluster": RelationMetaStub(
                        relation_name="cluster", role=RelationRoleStub.peer, interface_name="test-interface"
                    ),
                }
            ),
            model=ModelStub(relations={"cluster": [peer_relation]}),
            app=ApplicationStub(name="app"),
        )
        refs = {"5": PersistenceState(id=1, ref=1)}

        # WHEN
        results = runner.checkpoint_all(cast(ops.CharmBase, charm), refs)

        # THEN the peer relation is not resolved; the ref is reported as an ERROR instead of
        # silently checkpointing against a peer relation
        assert len(results.results) == 1
        assert results.results[0].status == "ERROR"
        assert "not found" in (results.results[0].error or "")
        assert results.updated_refs == {}

    def test_checkpoint_all_captures_exception_as_error_result(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", ExplodingPersistenceValidator)
        relation = RelationStub(name="db", id=5)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)
        refs = {"5": PersistenceState(id=1, ref=1)}

        # WHEN
        results = runner.checkpoint_all(cast(ops.CharmBase, charm), refs)

        # THEN
        assert len(results.results) == 1
        assert results.results[0].status == "ERROR"
        assert "checkpoint exploded" in (results.results[0].error or "")
        assert results.updated_refs == {}

    def test_cleanup_all_calls_cleanup_on_every_target(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", PreparingPersistenceValidator)
        relation = RelationStub(name="db", id=5)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.cleanup_all(cast(ops.CharmBase, charm))

        # THEN
        assert results.results == []
        assert results.updated_refs == {}
        assert results.cleaned_relation_ids == [5]
        assert PreparingPersistenceValidator.cleanup_calls == [5]

    def test_cleanup_all_captures_exception_as_error_result(self) -> None:
        # GIVEN
        runner = self._runner_with("test-interface", ExplodingPersistenceValidator)
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.cleanup_all(cast(ops.CharmBase, charm))

        # THEN
        assert len(results.results) == 1
        assert results.results[0].status == "ERROR"
        assert "cleanup exploded" in (results.results[0].error or "")
        assert results.cleaned_relation_ids == [0]

    def test_persistence_targets_ignored_when_interface_has_no_registered_validator(self) -> None:
        # GIVEN a runner with no persistence validators registered at all
        runner = ValidatorRunner.__new__(ValidatorRunner)
        runner.validators = {}
        runner.persistence_validators = {}
        runner.persistence_load_errors = {}
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        prepare_results = runner.prepare_all(cast(ops.CharmBase, charm))
        cleanup_results = runner.cleanup_all(cast(ops.CharmBase, charm))

        # THEN
        assert prepare_results.results == []
        assert prepare_results.updated_refs == {}
        assert cleanup_results.results == []
        assert cleanup_results.cleaned_relation_ids == []

    def test_prepare_all_reports_error_for_interface_with_load_error(self) -> None:
        # GIVEN a relation on an interface whose persistence validator failed to load
        runner = ValidatorRunner.__new__(ValidatorRunner)
        runner.validators = {}
        runner.persistence_validators = {}
        runner.persistence_load_errors = {"test-interface": "boom"}
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.prepare_all(cast(ops.CharmBase, charm))

        # THEN the load failure surfaces as an ERROR instead of being silently skipped, so
        # post_persistence doesn't mistake "nothing loaded" for "nothing to do"
        assert len(results.results) == 1
        assert results.results[0].status == "ERROR"
        assert "failed to load" in (results.results[0].error or "")

    def test_cleanup_all_reports_error_for_interface_with_load_error(self) -> None:
        # GIVEN a relation on an interface whose persistence validator failed to load
        runner = ValidatorRunner.__new__(ValidatorRunner)
        runner.validators = {}
        runner.persistence_validators = {}
        runner.persistence_load_errors = {"test-interface": "boom"}
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.cleanup_all(cast(ops.CharmBase, charm))

        # THEN cleanup reports an ERROR rather than an empty (and therefore state-clearing)
        # result list
        assert len(results.results) == 1
        assert results.results[0].status == "ERROR"
        assert "failed to load" in (results.results[0].error or "")
        # The relation was never visited by cleanup_all itself (no validator registered for it),
        # so it must not appear in cleaned_relation_ids either.
        assert results.cleaned_relation_ids == []

    def test_checkpoint_all_reports_error_when_ref_interface_has_no_registered_validator(self) -> None:
        # GIVEN a ref pointing at a live relation whose interface has no registered validator
        # (e.g. it was removed since the ref was produced, or failed to load this run)
        runner = ValidatorRunner.__new__(ValidatorRunner)
        runner.validators = {}
        runner.persistence_validators = {}
        runner.persistence_load_errors = {}
        relation = RelationStub(name="db", id=7)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.checkpoint_all(cast(ops.CharmBase, charm), refs={"7": PersistenceState(id=1, ref=1)})

        # THEN the checkpoint is reported as an ERROR rather than silently succeeding with no
        # results, which would let a real durability check pass without ever running
        assert len(results.results) == 1
        assert results.results[0].status == "ERROR"
        assert "No persistence validator registered" in (results.results[0].error or "")
        assert results.updated_refs == {}

    def test_checkpoint_all_does_not_duplicate_error_for_ref_on_interface_with_load_error(self) -> None:
        # GIVEN a ref pointing at a live relation whose interface's persistence validator failed
        # to load this run
        # Regression test for: _persistence_load_error_results() already adds one ERROR per live
        # relation on a load-failed interface; the ref-checkpointing loop below used to add a
        # second, more generic ERROR for the same relation_id, reporting one load failure twice.
        runner = ValidatorRunner.__new__(ValidatorRunner)
        runner.validators = {}
        runner.persistence_validators = {}
        runner.persistence_load_errors = {"test-interface": "boom"}
        relation = RelationStub(name="db", id=7)
        charm = make_charm_from_relation(relation, interface_name="test-interface", role=RelationRoleStub.requires)

        # WHEN
        results = runner.checkpoint_all(cast(ops.CharmBase, charm), refs={"7": PersistenceState(id=1, ref=1)})

        # THEN only the single load-error ERROR is reported for this relation, not two
        assert len(results.results) == 1
        assert results.results[0].status == "ERROR"
        assert "failed to load" in (results.results[0].error or "")
        assert results.updated_refs == {}


class TestConfigureLogging:
    """Tests for the file logging set up on the "validators" logger."""

    @pytest.fixture(autouse=True)
    def _clean_validators_logger(self) -> Iterator[None]:
        # Snapshot full logger state up front so it can be restored exactly, not just
        # the handlers - level/propagate are also mutated by _configure_logging() and
        # must not leak into other test modules sharing this module-level logger.
        original_level = logger.level
        original_propagate = logger.propagate
        original_handlers = list(logger.handlers)

        yield None

        # Always run, even if the test body fails partway through, so state never
        # leaks onto the shared module-level "validators" logger.
        for handler in list(logger.handlers):
            if handler not in original_handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    def test_writes_to_var_log_validators(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "validators"

        # WHEN
        runner._configure_logging(log_dir=log_dir)
        logger.info("hello from validators")

        # THEN the log directory and file are created, and the message is written
        assert log_dir.is_dir()
        assert (log_dir / "validator.log").exists()
        assert "hello from validators" in (log_dir / "validator.log").read_text()

    def test_falls_back_to_stderr_when_log_dir_not_writable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # GIVEN a log_dir path that already exists as a regular file, so mkdir()
        # deterministically fails with OSError regardless of the user running the test
        log_dir = tmp_path / "validators"
        log_dir.write_text("not a directory")

        # WHEN
        runner._configure_logging(log_dir=log_dir)
        logger.warning("fallback message")

        # THEN nothing raised, the warning ends up on stderr, and it's formatted
        # consistently with the file handler (level/timestamp), not bare text
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "fallback message" in captured.err

    def test_stdout_remains_valid_json_after_logging_configured(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "validators"

        runner._configure_logging(log_dir=log_dir)
        logger.info("some diagnostic noise")

        results = ValidatorRunnerResults(results=[])
        # THEN stdout-bound output (the JSON blob) contains no log noise
        output = results.model_dump_json()
        assert output == '{"results":[],"updated_refs":{},"cleaned_relation_ids":[]}'

    def test_does_not_propagate_to_root_logger(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "validators"

        # WHEN
        runner._configure_logging(log_dir=log_dir)

        # THEN records stay local to the "validators" logger and never reach the root
        # logger, which some hosts (e.g. ops/charm frameworks) attach a stdout handler to
        assert logger.propagate is False

    def test_reconfiguring_does_not_duplicate_handlers_or_log_lines(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "validators"

        # WHEN configured twice, as could happen across multiple runs in one process
        runner._configure_logging(log_dir=log_dir)
        runner._configure_logging(log_dir=log_dir)
        logger.info("single message")

        # THEN only one handler is attached, and the message appears exactly once
        assert len(logger.handlers) == 1
        log_contents = (log_dir / "validator.log").read_text()
        assert log_contents.count("single message") == 1

    def test_does_not_close_externally_attached_handlers(self, tmp_path: Path) -> None:
        # GIVEN a host application has already attached its own handler to the
        # "validators" logger before this module configures its own logging
        external_handler = logging.NullHandler()
        logger.addHandler(external_handler)

        # WHEN
        runner._configure_logging(log_dir=tmp_path / "validators")

        # THEN the externally-attached handler is left untouched (not removed or closed),
        # alongside the handler this module installs
        assert external_handler in logger.handlers
        assert len(logger.handlers) == 2
