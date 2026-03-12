# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from extensions.validator_injection.extension import (
    ValidatorInjectorExtension,
    remote_validators_path,
)
from juju.backend import JujuExecOutput

from validators.base import ValidationResult
from validators.runner import ValidatorRunnerResults

from ..shared import NullJujuBackend

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class JujuStub(NullJujuBackend):
    """JujuBackend stub with a response queue for exec_unit and captured calls."""

    exec_responses: deque[JujuExecOutput] = field(default_factory=deque)
    exec_calls: list[tuple[str, str, str]] = field(default_factory=list)
    scp_calls: list[tuple[str, str, str]] = field(default_factory=list)
    units_by_app: dict[str, list[str]] = field(default_factory=dict)

    def application_units(self, model: str, application: str) -> list[str]:
        return self.units_by_app.get(application, [])

    def exec_unit(self, model: str, unit: str, task: str) -> JujuExecOutput:
        self.exec_calls.append((model, unit, task))
        if self.exec_responses:
            return self.exec_responses.popleft()
        return JujuExecOutput(return_code=0, stdout="", stderr="")

    def scp(self, model: str, source: str, destination: str) -> None:
        self.scp_calls.append((model, source, destination))

    def ssh(self, model: str, unit: str, cmd: str) -> None:
        pass


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


def _pass_result(endpoint: str = "db") -> ValidationResult:
    return ValidationResult(
        status="PASS", endpoint=endpoint, interface="sample_interface", level="simple", relation_id=0
    )


def _fail_result(endpoint: str = "db") -> ValidationResult:
    return ValidationResult(
        status="FAIL", endpoint=endpoint, interface="sample_interface", level="simple", relation_id=0
    )


def _error_result(endpoint: str = "db", error: str = "oops") -> ValidationResult:
    return ValidationResult(
        status="ERROR", endpoint=endpoint, interface="sample_interface", level="simple", relation_id=0, error=error
    )


# Exec responses for a full injection + clean run cycle:
#   1. test -f venv_runner → rc=1 (not present)
#   2-4. three install commands → rc=0 each
#   5. run_validators       → rc=0 with PASS JSON
def _inject_and_pass_responses(run_stdout: str | None = None) -> list[JujuExecOutput]:
    if run_stdout is None:
        run_stdout = _runner_json(_pass_result())
    return [_fail(), _ok(), _ok(), _ok(), _ok(run_stdout)]


# Exec responses when the venv is already installed:
#   1. test -f venv_runner → rc=0 (present)
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
            extension.post_validate("mymodel", "myapp", "simple")

            # THEN exec_unit was called for both units
            units_called = {call[1] for call in juju.exec_calls}
            assert "myapp/0" in units_called
            assert "myapp/1" in units_called

        def test_does_nothing_when_no_units(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN no units in the application
            juju.units_by_app["myapp"] = []

            # WHEN
            extension.post_validate("mymodel", "myapp", "simple")

            # THEN no exec calls were made
            assert juju.exec_calls == []

    class TestRunValidatorsOnUnit:
        class TestVenvAlreadyInstalled:
            def test_skips_injection_and_runs_validators(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN the venv is already present and the run succeeds
                juju.exec_responses.extend(_preinstalled_responses())

                # WHEN
                extension._run_validators_on_unit("mymodel", "myapp/0", "simple")

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
                    extension._run_validators_on_unit("mymodel", "myapp/0", "simple")

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
                extension_no_path._run_validators_on_unit("mymodel", "myapp/0", "simple")

                # THEN a warning is logged and no further exec calls happen
                assert len(juju.exec_calls) == 1
                assert any("skipping" in w.lower() for w in logger.warnings)

            def test_injects_and_runs_when_validators_path_set(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN the venv is absent and a validators_path is configured
                juju.exec_responses.extend(_inject_and_pass_responses())

                # WHEN
                extension._run_validators_on_unit("mymodel", "myapp/0", "simple")

                # THEN scp + 3 install commands + run_validators all happened
                assert len(juju.scp_calls) == 2  # validators + uv
                assert len(juju.exec_calls) == 5  # test-f + 3 installs + run

        class TestResultHandling:
            def test_does_not_raise_when_all_pass(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
                # GIVEN two endpoints both pass
                run_stdout = _runner_json(_pass_result("db"), _pass_result("monitoring"))
                juju.exec_responses.extend(_preinstalled_responses(run_stdout))

                # WHEN / THEN (no exception raised)
                extension._run_validators_on_unit("mymodel", "myapp/0", "simple")

            def test_raises_naming_the_failing_endpoint(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN one endpoint fails
                run_stdout = _runner_json(_fail_result("db"))
                juju.exec_responses.extend(_preinstalled_responses(run_stdout))

                # WHEN / THEN
                with pytest.raises(RuntimeError, match="db"):
                    extension._run_validators_on_unit("mymodel", "myapp/0", "simple")

            def test_raises_listing_all_failing_endpoints(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN two different endpoints fail
                run_stdout = _runner_json(_fail_result("db"), _fail_result("metrics"))
                juju.exec_responses.extend(_preinstalled_responses(run_stdout))

                # WHEN / THEN
                with pytest.raises(RuntimeError) as exc_info:
                    extension._run_validators_on_unit("mymodel", "myapp/0", "simple")
                assert "db" in str(exc_info.value)
                assert "metrics" in str(exc_info.value)

            def test_passes_level_to_runner_command(
                self, extension: ValidatorInjectorExtension, juju: JujuStub
            ) -> None:
                # GIVEN venv already present
                juju.exec_responses.extend(_preinstalled_responses())

                # WHEN running at "deep" level
                extension._run_validators_on_unit("mymodel", "myapp/0", "deep")

                # THEN the runner command includes --level deep
                run_cmd = juju.exec_calls[-1][2]
                assert "--level deep" in run_cmd

            def test_logs_error_message_for_failing_endpoint(
                self,
                extension: ValidatorInjectorExtension,
                juju: JujuStub,
                logger: LoggerStub,
            ) -> None:
                # GIVEN an endpoint that returns an ERROR with a message
                run_stdout = _runner_json(_error_result("db", error="connection refused"))
                juju.exec_responses.extend(_preinstalled_responses(run_stdout))

                # WHEN
                with pytest.raises(RuntimeError):
                    extension._run_validators_on_unit("mymodel", "myapp/0", "simple")

                # THEN the error message is logged
                assert any("connection refused" in e for e in logger.errors)

    class TestInjectValidators:
        def test_raises_when_validators_path_is_none(self, extension_no_path: ValidatorInjectorExtension) -> None:
            # GIVEN no validators_path configured
            # WHEN / THEN
            with pytest.raises(ValueError, match="validators_path"):
                extension_no_path._inject_validators("mymodel", "myapp/0")

        def test_calls_scp_with_resolved_path(
            self,
            extension: ValidatorInjectorExtension,
            juju: JujuStub,
            validators_path: Path,
        ) -> None:
            # GIVEN all install commands succeed
            juju.exec_responses.extend([_ok(), _ok(), _ok()])

            # WHEN
            extension._inject_validators("mymodel", "myapp/0")

            # THEN scp is called with the resolved source path and correct destination
            assert len(juju.scp_calls) == 2  # validators + uv
            _, source, dest = juju.scp_calls[0]
            assert source == str(validators_path.resolve())
            assert dest == f"myapp/0:{remote_validators_path}/packages"

        def test_runs_three_install_commands(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN all install commands succeed
            juju.exec_responses.extend([_ok(), _ok(), _ok()])

            # WHEN
            extension._inject_validators("mymodel", "myapp/0")

            # THEN exactly three exec_unit calls were made
            assert len(juju.exec_calls) == 3

        def test_raises_when_apt_install_fails(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN chmod +x uv fails
            juju.exec_responses.append(_fail(stderr="chmod error"))

            # WHEN / THEN
            with pytest.raises(RuntimeError, match="make uv executable"):
                extension._inject_validators("mymodel", "myapp/0")

        def test_raises_when_venv_creation_fails(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN apt-get succeeds but venv creation fails
            juju.exec_responses.extend([_ok(), _fail(stderr="venv error")])

            # WHEN / THEN
            with pytest.raises(RuntimeError, match="create venv"):
                extension._inject_validators("mymodel", "myapp/0")

        def test_raises_when_pip_install_fails(self, extension: ValidatorInjectorExtension, juju: JujuStub) -> None:
            # GIVEN apt-get and venv succeed but pip install fails
            juju.exec_responses.extend([_ok(), _ok(), _fail(stderr="pip error")])

            # WHEN / THEN
            with pytest.raises(RuntimeError, match="install validator packages"):
                extension._inject_validators("mymodel", "myapp/0")
