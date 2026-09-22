# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from extensions.validator_injection.extension import (
    ValidatorInjectorExtension,
    persistence_marker,
    remote_validators_path,
)
from juju import JujuModelHandle, PersistenceKey
from juju.backend import JujuExecOutput

from validators.base import PersistenceState, ValidationResult
from validators.runner import ValidatorRunnerResults

from ..shared import NullJujuBackend

TEST_TOKEN = "test-token"

TEST_MODEL: JujuModelHandle = JujuModelHandle(controller="test-controller", model="mymodel")

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class JujuStub(NullJujuBackend):
    """JujuBackend stub with a response queue for exec_unit and captured calls."""

    exec_responses: deque[JujuExecOutput] = field(default_factory=deque)
    exec_calls: list[tuple[JujuModelHandle, str, str, bool]] = field(default_factory=list)
    scp_calls: list[tuple[JujuModelHandle, str, str]] = field(default_factory=list)
    ssh_calls: list[tuple[JujuModelHandle, str, str]] = field(default_factory=list)
    units_by_app: dict[str, list[str]] = field(default_factory=dict)
    k8s_models: set[str] = field(default_factory=set)

    def application_units(self, model: JujuModelHandle, application: str) -> list[str]:
        return self.units_by_app.get(application, [])

    def exec_unit(self, model: JujuModelHandle, unit: str, task: str, operator: bool = False) -> JujuExecOutput:
        self.exec_calls.append((model, unit, task, operator))
        if self.exec_responses:
            return self.exec_responses.popleft()
        return JujuExecOutput(return_code=0, stdout="", stderr="")

    def scp(self, model: JujuModelHandle, source: str, destination: str) -> None:
        self.scp_calls.append((model, source, destination))

    def ssh(self, model: JujuModelHandle, unit: str, cmd: str) -> None:
        self.ssh_calls.append((model, unit, cmd))

    def is_k8s_model(self, model: JujuModelHandle) -> bool:
        return model.model in self.k8s_models


class LoggerStub(logging.Logger):
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.debugs: list[str] = []
        self.errors: list[str] = []

    def warning(self, msg: str, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        self.warnings.append(str(msg))

    def debug(self, msg: str, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        self.debugs.append(str(msg))

    def error(self, msg: str, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        self.errors.append(str(msg))

    def getChild(self, suffix: str) -> "LoggerStub":
        return self


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout: str = "") -> JujuExecOutput:
    return JujuExecOutput(return_code=0, stdout=stdout, stderr="")


def _fail(stderr: str = "error") -> JujuExecOutput:
    return JujuExecOutput(return_code=1, stdout="", stderr=stderr)


def _runner_json(*results: ValidationResult) -> str:
    return ValidatorRunnerResults(results=list(results)).model_dump_json()


def _pass_result(endpoint: str = "db", relation_id: int = 0) -> ValidationResult:
    return ValidationResult(
        status="PASS",
        endpoint=endpoint,
        interface="sample_interface",
        level="simple",
        role="requires",
        relation_id=relation_id,
    )


def _fail_result(endpoint: str = "db", relation_id: int = 0) -> ValidationResult:
    return ValidationResult(
        status="FAIL",
        endpoint=endpoint,
        interface="sample_interface",
        level="simple",
        role="requires",
        relation_id=relation_id,
    )


def _error_result(endpoint: str = "db", error: str = "oops", relation_id: int = 0) -> ValidationResult:
    return ValidationResult(
        status="ERROR",
        endpoint=endpoint,
        interface="sample_interface",
        level="simple",
        role="requires",
        relation_id=relation_id,
        error=error,
    )


def _persistence_runner_json(
    results: list[ValidationResult] | None = None,
    updated_refs: dict[str, PersistenceState] | None = None,
    cleaned_relation_ids: list[int] | None = None,
) -> str:
    return ValidatorRunnerResults(
        results=results or [], updated_refs=updated_refs or {}, cleaned_relation_ids=cleaned_relation_ids or []
    ).model_dump_json()


# Exec responses for a full injection + clean run cycle:
#   1. test -f venv_runner [&& test -f persistence_marker] → rc=1 (not present)
#   2-5. four install commands (chmod, venv, pip install, touch marker) → rc=0 each
#   6. run_validators       → rc=0 with PASS JSON
def _inject_and_pass_responses(run_stdout: str | None = None) -> list[JujuExecOutput]:
    if run_stdout is None:
        run_stdout = _runner_json(_pass_result())
    return [_fail(), _ok(), _ok(), _ok(), _ok(), _ok(run_stdout)]


# Exec responses when the venv is already installed:
#   1. test -f venv_runner [&& test -f persistence_marker] → rc=0 (present)
#   2. run_validators      → rc=0 with PASS JSON
def _preinstalled_responses(run_stdout: str | None = None) -> list[JujuExecOutput]:
    if run_stdout is None:
        run_stdout = _runner_json(_pass_result())
    return [_ok(), _ok(run_stdout)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestValidatorInjectorExtension:
    @pytest.fixture
    def juju(self) -> JujuStub:
        return JujuStub()

    @pytest.fixture
    def logger(self) -> LoggerStub:
        return LoggerStub()

    @pytest.fixture
    def validators_path(self, tmp_path: Path) -> Path:
        return tmp_path / "validators"

    @pytest.fixture
    def uv_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "uv"
        path.write_bytes(b"")
        return path

    @pytest.fixture
    def extension(
        self, juju: JujuStub, logger: LoggerStub, validators_path: Path, uv_file: Path
    ) -> ValidatorInjectorExtension:
        return ValidatorInjectorExtension(validators_path=validators_path, juju=juju, logger=logger, uv_file=uv_file)

    @pytest.fixture
    def extension_no_path(self, juju: JujuStub, logger: LoggerStub, uv_file: Path) -> ValidatorInjectorExtension:
        return ValidatorInjectorExtension(validators_path=None, juju=juju, logger=logger, uv_file=uv_file)

    class TestPostValidate:
        def test_runs_validators_on_each_unit(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN two units in the application
            juju.units_by_app["myapp"] = ["myapp/0", "myapp/1"]
            for _ in ["myapp/0", "myapp/1"]:
                juju.exec_responses.extend(_inject_and_pass_responses())

            # WHEN
            extension.post_validate(TEST_MODEL, "myapp", "simple")

            # THEN exec_unit was called for both units
            units_called = {call[1] for call in juju.exec_calls}
            assert "myapp/0" in units_called
            assert "myapp/1" in units_called

        def test_does_nothing_when_no_units(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN no units in the application
            juju.units_by_app["myapp"] = []

            # WHEN
            extension.post_validate(TEST_MODEL, "myapp", "simple")

            # THEN no exec calls were made
            assert juju.exec_calls == []

        def test_returns_results_keyed_by_unit(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN two units, each returning one PASS result
            juju.units_by_app["myapp"] = ["myapp/0", "myapp/1"]
            run_stdout = _runner_json(_pass_result("db"))
            for _ in ["myapp/0", "myapp/1"]:
                juju.exec_responses.extend(_preinstalled_responses(run_stdout))

            # WHEN
            results = extension.post_validate(TEST_MODEL, "myapp", "simple")

            # THEN the return value maps each unit to its list of results
            assert set(results.keys()) == {"myapp/0", "myapp/1"}
            assert all(len(v) == 1 for v in results.values())

    class TestPostPersistence:
        def test_rejects_unsupported_persistence_op(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # GIVEN a persistence op outside the supported set (post_persistence's `persistence`
            # parameter is a plain str; JujuClient.validate_model's Literal type isn't enforced at
            # runtime, so a bad value here must be rejected before being interpolated into a
            # remote shell command rather than silently run).
            juju.units_by_app["myapp"] = ["myapp/0"]

            # WHEN / THEN
            with pytest.raises(ValueError, match="Unsupported persistence op"):
                extension.post_persistence(TEST_MODEL, "myapp", "prepare; rm -rf /", {})

            # THEN no command was ever run on the unit
            assert not juju.exec_calls

        def test_reinstalls_when_venv_predates_persistence_support(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # Regression test for: a venv installed before persistence support exists has
            # venv_runner present but understands only --level. The readiness check must also
            # require the persistence_marker file, or this venv is invoked with an unsupported arg.
            juju.units_by_app["myapp"] = ["myapp/0"]
            juju.exec_responses.extend(_inject_and_pass_responses(_persistence_runner_json()))

            # WHEN
            extension.post_persistence(TEST_MODEL, "myapp", "prepare", {})

            # THEN the venv was (re)installed - i.e. more than just the readiness check and the run
            # command were executed - and the run command still succeeded afterwards
            run_cmds = [call[2] for call in juju.exec_calls if "--persistence" in call[2]]
            assert len(run_cmds) == 1
            assert len(juju.exec_calls) > 2

        def test_prepare_runs_on_each_unit_with_no_refs_argument(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # GIVEN two units and no prior persistence state
            juju.units_by_app["myapp"] = ["myapp/0", "myapp/1"]
            for _ in ["myapp/0", "myapp/1"]:
                juju.exec_responses.extend(_preinstalled_responses(_persistence_runner_json()))

            # WHEN
            extension.post_persistence(TEST_MODEL, "myapp", "prepare", {})

            # THEN each unit's run command has --persistence prepare and no --refs
            run_cmds = [call[2] for call in juju.exec_calls if "--persistence" in call[2]]
            assert len(run_cmds) == 2
            for cmd in run_cmds:
                assert "--persistence prepare" in cmd
                assert "--refs" not in cmd

        def test_prepare_updates_persistence_state_with_returned_refs(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # GIVEN a unit whose prepare run returns a new ref for relation 4
            juju.units_by_app["myapp"] = ["myapp/0"]
            new_state = PersistenceState(id=99, ref=1, token=TEST_TOKEN)
            juju.exec_responses.extend(_preinstalled_responses(_persistence_runner_json(updated_refs={"4": new_state})))
            state: dict[PersistenceKey, PersistenceState] = {}

            # WHEN
            extension.post_persistence(TEST_MODEL, "myapp", "prepare", state)

            # THEN the state dict was updated with a key scoped to controller/model/unit
            key = PersistenceKey(
                controller=TEST_MODEL.controller, model=TEST_MODEL.model, unit="myapp/0", relation_id=4
            )
            assert state == {key: new_state}

        def test_checkpoint_passes_refs_scoped_to_the_unit(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # GIVEN persistence state for two different units
            juju.units_by_app["myapp"] = ["myapp/0", "myapp/1"]
            state_0 = PersistenceState(id=1, ref=2, token=TEST_TOKEN)
            state_1 = PersistenceState(id=2, ref=3, token=TEST_TOKEN)
            persistence_state = {
                PersistenceKey(TEST_MODEL.controller, TEST_MODEL.model, "myapp/0", 4): state_0,
                PersistenceKey(TEST_MODEL.controller, TEST_MODEL.model, "myapp/1", 5): state_1,
            }
            for _ in ["myapp/0", "myapp/1"]:
                juju.exec_responses.extend(_preinstalled_responses(_persistence_runner_json()))

            # WHEN
            extension.post_persistence(TEST_MODEL, "myapp", "checkpoint", persistence_state)

            # THEN each unit's run command only includes its own refs
            run_cmds = {call[1]: call[2] for call in juju.exec_calls if "--persistence" in call[2]}
            refs_0 = json.loads(run_cmds["myapp/0"].split("--refs ", 1)[1].strip("'"))
            refs_1 = json.loads(run_cmds["myapp/1"].split("--refs ", 1)[1].strip("'"))
            assert refs_0 == {"4": {"id": 1, "ref": 2, "token": TEST_TOKEN}}
            assert refs_1 == {"5": {"id": 2, "ref": 3, "token": TEST_TOKEN}}

        def test_cleanup_drops_state_for_the_unit_and_omits_refs(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # GIVEN persistence state tracked for the unit being cleaned up
            juju.units_by_app["myapp"] = ["myapp/0"]
            key = PersistenceKey(TEST_MODEL.controller, TEST_MODEL.model, "myapp/0", 4)
            persistence_state = {key: PersistenceState(id=1, ref=2, token=TEST_TOKEN)}
            juju.exec_responses.extend(_preinstalled_responses(_persistence_runner_json(cleaned_relation_ids=[4])))

            # WHEN
            extension.post_persistence(TEST_MODEL, "myapp", "cleanup", persistence_state)

            # THEN the state entry is dropped, and no --refs flag was sent
            assert persistence_state == {}
            run_cmd = juju.exec_calls[-1][2]
            assert "--persistence cleanup" in run_cmd
            assert "--refs" not in run_cmd

        def test_cleanup_keeps_state_when_a_result_is_fail_or_error(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # GIVEN cleanup visited relation 4 but reported a FAIL for its canary table
            juju.units_by_app["myapp"] = ["myapp/0"]
            key = PersistenceKey(TEST_MODEL.controller, TEST_MODEL.model, "myapp/0", 4)
            persistence_state = {key: PersistenceState(id=1, ref=2, token=TEST_TOKEN)}
            juju.exec_responses.extend(
                _preinstalled_responses(
                    _persistence_runner_json(results=[_fail_result("canary", relation_id=4)], cleaned_relation_ids=[4])
                )
            )

            # WHEN
            extension.post_persistence(TEST_MODEL, "myapp", "cleanup", persistence_state)

            # THEN the tracking entry is kept, since the canary data may not actually be gone
            assert persistence_state == {key: PersistenceState(id=1, ref=2, token=TEST_TOKEN)}

        def test_cleanup_keeps_state_for_a_relation_cleanup_never_visited(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # GIVEN two tracked relations on the same unit, but cleanup_all only visited one of
            # them (e.g. the other relation was already removed, or its interface's persistence
            # validator failed to load)
            juju.units_by_app["myapp"] = ["myapp/0"]
            visited_key = PersistenceKey(TEST_MODEL.controller, TEST_MODEL.model, "myapp/0", 4)
            unvisited_key = PersistenceKey(TEST_MODEL.controller, TEST_MODEL.model, "myapp/0", 9)
            persistence_state = {
                visited_key: PersistenceState(id=1, ref=2, token=TEST_TOKEN),
                unvisited_key: PersistenceState(id=2, ref=3, token=TEST_TOKEN),
            }
            juju.exec_responses.extend(_preinstalled_responses(_persistence_runner_json(cleaned_relation_ids=[4])))

            # WHEN
            extension.post_persistence(TEST_MODEL, "myapp", "cleanup", persistence_state)

            # THEN only the visited relation's state is dropped; the unvisited one is kept so its
            # (possibly still-present) canary data isn't silently forgotten
            assert persistence_state == {unvisited_key: PersistenceState(id=2, ref=3, token=TEST_TOKEN)}

        def test_cleanup_does_not_drop_state_belonging_to_other_units(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # GIVEN persistence state for this app's unit and an unrelated unit
            juju.units_by_app["myapp"] = ["myapp/0"]
            own_key = PersistenceKey(TEST_MODEL.controller, TEST_MODEL.model, "myapp/0", 4)
            other_key = PersistenceKey(TEST_MODEL.controller, TEST_MODEL.model, "otherapp/0", 7)
            persistence_state = {
                own_key: PersistenceState(id=1, ref=2, token=TEST_TOKEN),
                other_key: PersistenceState(id=2, ref=3, token=TEST_TOKEN),
            }
            juju.exec_responses.extend(_preinstalled_responses(_persistence_runner_json(cleaned_relation_ids=[4])))

            # WHEN
            extension.post_persistence(TEST_MODEL, "myapp", "cleanup", persistence_state)

            # THEN only this unit's entry is removed
            assert persistence_state == {other_key: PersistenceState(id=2, ref=3, token=TEST_TOKEN)}

        def test_returns_results_keyed_by_unit(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN one unit whose checkpoint run returns a FAIL result
            juju.units_by_app["myapp"] = ["myapp/0"]
            juju.exec_responses.extend(
                _preinstalled_responses(_persistence_runner_json(results=[_fail_result("canary")]))
            )

            # WHEN
            results = extension.post_persistence(TEST_MODEL, "myapp", "checkpoint", {})

            # THEN the failing result is returned under its unit
            assert results["myapp/0"][0].endpoint == "canary"
            assert results["myapp/0"][0].status == "FAIL"

        def test_continues_to_remaining_units_when_one_unit_remote_command_fails(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # Regression test for: _run_persistence_on_unit() raising (e.g. a non-zero
            # run_validators exit) previously propagated straight out of post_persistence(),
            # aborting the loop and leaving every unit after the failing one with no persistence
            # op attempted at all.
            juju.units_by_app["myapp"] = ["myapp/0", "myapp/1"]
            juju.exec_responses.extend([_ok(), _fail(stderr="boom")])  # myapp/0: readiness OK, run fails
            juju.exec_responses.extend(_preinstalled_responses(_persistence_runner_json()))  # myapp/1: succeeds

            # WHEN
            results = extension.post_persistence(TEST_MODEL, "myapp", "checkpoint", {})

            # THEN myapp/0 reports an ERROR result instead of raising, and myapp/1 was still
            # attempted (its own run command shows up in exec_calls) and reports its own result.
            assert results["myapp/0"][0].status == "ERROR"
            assert "boom" in (results["myapp/0"][0].error or "")
            run_cmds = [call for call in juju.exec_calls if "--persistence" in call[2]]
            assert len(run_cmds) == 2
            assert results["myapp/1"] == []

        def test_reports_error_and_does_not_mutate_state_for_a_malformed_relation_id(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # Regression test for: converting updated_refs' string keys to int ran outside the
            # per-unit try/except, so a malformed remote payload (non-numeric relation_id) raised
            # ValueError straight out of post_persistence() and aborted the loop early.
            juju.units_by_app["myapp"] = ["myapp/0", "myapp/1"]
            existing_key = PersistenceKey(TEST_MODEL.controller, TEST_MODEL.model, "myapp/0", 4)
            persistence_state = {existing_key: PersistenceState(id=1, ref=2, token=TEST_TOKEN)}
            malformed_stdout = (
                '{"results": [], "updated_refs": {"not-an-int": {"id": 1, "ref": 2, "token": "t"}}, '
                '"cleaned_relation_ids": []}'
            )
            juju.exec_responses.extend(_preinstalled_responses(malformed_stdout))  # myapp/0
            juju.exec_responses.extend(_preinstalled_responses(_persistence_runner_json()))  # myapp/1

            # WHEN
            results = extension.post_persistence(TEST_MODEL, "myapp", "checkpoint", persistence_state)

            # THEN myapp/0 reports an ERROR result instead of raising, its existing tracked state
            # is untouched, and myapp/1 still gets its own attempt.
            assert results["myapp/0"][0].status == "ERROR"
            assert persistence_state == {existing_key: PersistenceState(id=1, ref=2, token=TEST_TOKEN)}
            run_cmds = [call for call in juju.exec_calls if "--persistence" in call[2]]
            assert len(run_cmds) == 2
            assert results["myapp/1"] == []

        def test_skips_and_preserves_tracked_state_when_no_validators_path_and_venv_absent(
            self, extension_no_path: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # GIVEN the venv is absent and no validators_path is configured to inject one - this
            # is the normal state for a run where no validators (functional or persistence) are
            # configured at all, so it must be a silent skip (matching
            # _run_validators_on_unit's convention), not a hard failure.
            juju.units_by_app["myapp"] = ["myapp/0"]
            key = PersistenceKey(TEST_MODEL.controller, TEST_MODEL.model, "myapp/0", 4)
            persistence_state = {key: PersistenceState(id=1, ref=2, token=TEST_TOKEN)}
            juju.exec_responses.append(_fail())

            # WHEN cleanup is requested but cannot run
            results = extension_no_path.post_persistence(TEST_MODEL, "myapp", "cleanup", persistence_state)

            # THEN no results are reported, and the tracked state is preserved rather than deleted -
            # deleting it here would mean a cleanup that never ran (and so never dropped the real
            # canary data) is treated as having succeeded.
            assert results == {"myapp/0": []}
            assert persistence_state == {key: PersistenceState(id=1, ref=2, token=TEST_TOKEN)}

    class TestRunValidatorsOnUnit:
        class TestVenvAlreadyInstalled:
            def test_skips_injection_and_runs_validators(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN the venv is already present and the run succeeds
                juju.exec_responses.extend(_preinstalled_responses())

                # WHEN
                extension._run_validators_on_unit(TEST_MODEL, "myapp/0", "simple")

                # THEN only test-f + run_validators were called; no scp
                assert len(juju.exec_calls) == 2
                assert juju.scp_calls == []

            def test_raises_when_runner_exits_nonzero(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN the venv is present but the runner crashes
                juju.exec_responses.extend([_ok(), _fail(stderr="crash")])

                # WHEN / THEN
                with pytest.raises(RuntimeError, match="Validators failed"):
                    extension._run_validators_on_unit(TEST_MODEL, "myapp/0", "simple")

        class TestVenvAbsent:
            def test_warns_and_skips_when_no_validators_path(
                self,
                extension_no_path: ValidatorInjectorExtension,
                juju: JujuStub,
                logger: LoggerStub,
            ) -> None:
                # GIVEN the venv is absent and no validators_path is configured
                juju.exec_responses.append(_fail())

                # WHEN
                extension_no_path._run_validators_on_unit(TEST_MODEL, "myapp/0", "simple")

                # THEN a warning is logged and no further exec calls happen
                assert len(juju.exec_calls) == 1
                assert any("skipping" in w.lower() for w in logger.warnings)

            def test_injects_and_runs_when_validators_path_set(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN the venv is absent and a validators_path is configured
                juju.exec_responses.extend(_inject_and_pass_responses())

                # WHEN
                extension._run_validators_on_unit(TEST_MODEL, "myapp/0", "simple")

                # THEN scp + 4 install commands + run_validators all happened
                assert len(juju.scp_calls) == 2  # validators + uv
                assert len(juju.exec_calls) == 6  # test-f + 4 installs (incl. persistence marker) + run

        class TestResultHandling:
            def test_does_not_raise_when_all_pass(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
                # GIVEN two endpoints both pass
                run_stdout = _runner_json(_pass_result("db"), _pass_result("monitoring"))
                juju.exec_responses.extend(_preinstalled_responses(run_stdout))

                # WHEN / THEN (no exception raised)
                extension._run_validators_on_unit(TEST_MODEL, "myapp/0", "simple")

            def test_returns_fail_result_naming_the_failing_endpoint(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN one endpoint fails
                run_stdout = _runner_json(_fail_result("db"))
                juju.exec_responses.extend(_preinstalled_responses(run_stdout))

                # WHEN
                results = extension._run_validators_on_unit(TEST_MODEL, "myapp/0", "simple")

                # THEN the failing result is returned
                assert len(results) == 1
                assert results[0].endpoint == "db"
                assert results[0].status == "FAIL"

            def test_returns_fail_results_listing_all_failing_endpoints(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN two different endpoints fail
                run_stdout = _runner_json(_fail_result("db"), _fail_result("metrics"))
                juju.exec_responses.extend(_preinstalled_responses(run_stdout))

                # WHEN
                results = extension._run_validators_on_unit(TEST_MODEL, "myapp/0", "simple")

                # THEN both failing results are returned
                endpoints = {r.endpoint for r in results}
                assert "db" in endpoints
                assert "metrics" in endpoints

            def test_passes_level_to_runner_command(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN venv already present
                juju.exec_responses.extend(_preinstalled_responses())

                # WHEN running at "deep" level
                extension._run_validators_on_unit(TEST_MODEL, "myapp/0", "deep")

                # THEN the runner command includes --level deep
                run_cmd = juju.exec_calls[-1][2]
                assert "--level deep" in run_cmd

            def test_returns_error_result_with_error_message(
                self,
                extension: ValidatorInjectorExtension,
                juju: JujuStub,
            ) -> None:
                # GIVEN an endpoint that returns an ERROR with a message
                run_stdout = _runner_json(_error_result("db", error="connection refused"))
                juju.exec_responses.extend(_preinstalled_responses(run_stdout))

                # WHEN
                results = extension._run_validators_on_unit(TEST_MODEL, "myapp/0", "simple")

                # THEN the error result is returned with its error message
                assert len(results) == 1
                assert results[0].status == "ERROR"
                assert results[0].error == "connection refused"

        class TestOperatorFlag:
            def test_passes_operator_true_when_k8s_model(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN a K8s model and the venv is already present
                juju.k8s_models.add(TEST_MODEL.model)
                juju.units_by_app["myapp"] = ["myapp/0"]
                juju.exec_responses.extend(_inject_and_pass_responses())

                # WHEN
                extension.post_validate(TEST_MODEL, "myapp", "simple")

                # THEN every exec_unit call was made with operator=True
                assert len(juju.exec_calls) > 0
                assert all(call[3] is True for call in juju.exec_calls)

            def test_passes_operator_false_when_not_k8s_model(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN a non-K8s model and the venv is already present
                juju.units_by_app["myapp"] = ["myapp/0"]
                juju.exec_responses.extend(_inject_and_pass_responses())

                # WHEN
                extension.post_validate(TEST_MODEL, "myapp", "simple")

                # THEN every exec_unit call was made with operator=False
                assert len(juju.exec_calls) > 0
                assert all(call[3] is False for call in juju.exec_calls)

    class TestInjectValidators:
        def test_raises_when_validators_path_is_none(self, extension_no_path: ValidatorInjectorExtension) -> None:
            # GIVEN no validators_path configured
            # WHEN / THEN
            with pytest.raises(ValueError, match="validators_path"):
                extension_no_path._inject_validators(TEST_MODEL, "myapp/0")

        def test_calls_scp_with_resolved_path(
            self,
            extension: ValidatorInjectorExtension,
            juju: JujuStub,
            validators_path: Path,
        ) -> None:
            # GIVEN all install commands succeed
            juju.exec_responses.extend([_ok(), _ok(), _ok()])

            # WHEN
            extension._inject_validators(TEST_MODEL, "myapp/0", is_k8s=True)

            # THEN scp is called with the resolved source path and correct destination
            assert len(juju.scp_calls) == 2  # validators + uv
            _, source, dest = juju.scp_calls[0]
            assert source == str(validators_path.resolve())
            assert dest == f"myapp/0:{remote_validators_path}/packages"

        def test_calls_ssh_mkdir_before_scp(
            self,
            extension: ValidatorInjectorExtension,
            juju: JujuStub,
        ) -> None:
            # GIVEN all install commands succeed
            juju.exec_responses.extend([_ok(), _ok(), _ok()])

            # WHEN
            extension._inject_validators(TEST_MODEL, "myapp/0", is_k8s=True)

            # THEN ssh was called to create the remote directory before copying files
            assert len(juju.ssh_calls) == 1
            _, unit, cmd = juju.ssh_calls[0]
            assert unit == "myapp/0"
            assert cmd == f"mkdir -p {remote_validators_path}"

        def test_calls_ssh_mkdir_before_scp_with_sudo_in_non_k8s_model_and_chowns_it(
            self,
            extension: ValidatorInjectorExtension,
            juju: JujuStub,
        ) -> None:
            # GIVEN all install commands succeed
            juju.exec_responses.extend([_ok(), _ok(), _ok()])

            # WHEN
            extension._inject_validators(TEST_MODEL, "myapp/0", is_k8s=False)

            # THEN ssh was called to create the remote directory before copying files
            assert len(juju.ssh_calls) == 1
            _, unit, cmd = juju.ssh_calls[0]
            mkdir, chown = cmd.split(" && ")
            assert unit == "myapp/0"
            assert mkdir == f"sudo mkdir -p {remote_validators_path}"
            assert chown == f"sudo chown -R $(id -u) {remote_validators_path}"

        def test_runs_four_install_commands(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN all install commands succeed
            juju.exec_responses.extend([_ok(), _ok(), _ok(), _ok()])

            # WHEN
            extension._inject_validators(TEST_MODEL, "myapp/0")

            # THEN exactly four exec_unit calls were made (chmod, venv, pip install, marker touch)
            assert len(juju.exec_calls) == 4

        def test_writes_persistence_marker_after_install(
            self, extension: ValidatorInjectorExtension, juju: JujuStub
        ) -> None:
            # GIVEN all install commands succeed
            juju.exec_responses.extend([_ok(), _ok(), _ok(), _ok()])

            # WHEN
            extension._inject_validators(TEST_MODEL, "myapp/0")

            # THEN the last command touches the persistence-capability marker, so a stale venv
            # from before persistence support existed can be told apart from a freshly (re)installed one
            assert juju.exec_calls[-1][2] == f"touch {persistence_marker}"

        def test_uv_commands_include_uv_no_cache(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN all install commands succeed
            juju.exec_responses.extend([_ok(), _ok(), _ok(), _ok()])

            # WHEN
            extension._inject_validators(TEST_MODEL, "myapp/0")

            # THEN the venv creation and pip install commands include UV_NO_CACHE=1
            # exec_calls[0] is chmod +x (doesn't need UV_NO_CACHE)
            # exec_calls[1] is uv venv, exec_calls[2] is uv pip install
            venv_cmd = juju.exec_calls[1][2]
            pip_cmd = juju.exec_calls[2][2]
            assert "UV_NO_CACHE=1" in venv_cmd, f"UV_NO_CACHE not in venv command: {venv_cmd}"
            assert "UV_NO_CACHE=1" in pip_cmd, f"UV_NO_CACHE not in pip install command: {pip_cmd}"

        def test_raises_when_apt_install_fails(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN chmod +x uv fails
            juju.exec_responses.append(_fail(stderr="chmod error"))

            # WHEN / THEN
            with pytest.raises(RuntimeError, match="make uv executable"):
                extension._inject_validators(TEST_MODEL, "myapp/0")

        def test_raises_when_venv_creation_fails(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN apt-get succeeds but venv creation fails
            juju.exec_responses.extend([_ok(), _fail(stderr="venv error")])

            # WHEN / THEN
            with pytest.raises(RuntimeError, match="create venv"):
                extension._inject_validators(TEST_MODEL, "myapp/0")

        def test_raises_when_pip_install_fails(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN apt-get and venv succeed but pip install fails
            juju.exec_responses.extend([_ok(), _ok(), _fail(stderr="pip error")])

            # WHEN / THEN
            with pytest.raises(RuntimeError, match="install validator packages"):
                extension._inject_validators(TEST_MODEL, "myapp/0")
