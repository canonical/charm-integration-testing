# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import jubilant
import pytest
from juju import JujuModelHandle, JujuWaitTimeoutError
from juju_cmd.cmd import CmdError
from juju_jubilant import JubilantBackend
from juju_jubilant.client import JubilantClient
from utils.timeouts import remaining_timeout, timeout_after


class ExecutableClient(JubilantClient):
    def __init__(self, executable: Path, query_timeout: timedelta = timedelta(seconds=60)) -> None:
        super().__init__(query_timeout)
        self.executable = executable

    def model(self, model: JujuModelHandle | None) -> jubilant.Juju:
        client = super().model(model)
        client.cli_binary = str(self.executable)
        return client


@pytest.fixture
def sleeping_juju(tmp_path: Path) -> Path:
    executable = tmp_path / "juju"
    executable.write_text(
        f"#!{sys.executable} -S\n"
        "import os, pathlib, time\n"
        f"pathlib.Path({str(tmp_path)!r}, str(os.getpid()) + '.pid').touch()\n"
        "time.sleep(5)\n"
        "print('{}')\n"
    )
    executable.chmod(0o755)
    return executable


def assert_processes_reaped(directory: Path) -> None:
    pids = list(directory.glob("*.pid"))
    assert pids
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid.stem), 0)


def test_query_timeout_kills_and_reaps_cli(sleeping_juju: Path) -> None:
    # GIVEN a Juju command that never responds within its query budget
    client = ExecutableClient(sleeping_juju, query_timeout=timedelta(milliseconds=200))

    # WHEN the query runs
    with pytest.raises(subprocess.TimeoutExpired):
        client.model(None).cli("status")

    # THEN the timed-out CLI is not left running
    assert_processes_reaped(sleeping_juju.parent)


def test_inherited_queries_use_bounded_transport(sleeping_juju: Path) -> None:
    # GIVEN a query still implemented by the deprecated backend
    backend = JubilantBackend(ExecutableClient(sleeping_juju, query_timeout=timedelta(milliseconds=200)))

    # WHEN the inherited implementation fetches status
    with pytest.raises(subprocess.TimeoutExpired):
        backend.num_units(JujuModelHandle(controller="controller", model="model"), "application")

    # THEN it uses the bounded CLI rather than the old command runner
    assert_processes_reaped(sleeping_juju.parent)


def test_inherited_queries_preserve_cli_error_type(tmp_path: Path) -> None:
    # GIVEN a command that fails with useful output
    executable = tmp_path / "juju"
    executable.write_text(f"#!{sys.executable}\nimport sys\nprint('unreachable', file=sys.stderr)\nsys.exit(1)\n")
    executable.chmod(0o755)
    backend = JubilantBackend(ExecutableClient(executable))

    # WHEN an inherited operation fails
    with pytest.raises(CmdError) as exc_info:
        backend.num_units(JujuModelHandle(controller="controller", model="model"), "application")

    # THEN its existing exception contract and diagnostics are preserved
    assert exc_info.value.returncode == 1
    assert exc_info.value.stderr == "unreachable\n"


@pytest.mark.parametrize("wait_kind", ["single-model", "multi-model", "model-exists"])
def test_polling_deadline_bounds_blocked_status(sleeping_juju: Path, wait_kind: str) -> None:
    # GIVEN status queries whose normal timeout exceeds the entire operation budget
    backend = JubilantBackend(ExecutableClient(sleeping_juju))
    models = [JujuModelHandle(controller="controller", model=name) for name in ("first", "second")]
    budget = timedelta(milliseconds=200)
    start = time.monotonic()

    # WHEN a status query blocks, including concurrent multi-model queries
    with pytest.raises(JujuWaitTimeoutError, match="Timed out querying model"):
        if wait_kind == "multi-model":
            backend.wait_idle_multi_model(models, timeout=budget, count=1, strict_timeout=True)
        elif wait_kind == "single-model":
            backend.wait_idle(models[0], timeout=budget, count=1, strict_timeout=True)
        else:
            backend.wait_for_model_to_exist(models[0], timeout=budget)

    # THEN the operation deadline interrupts the CLI, including executor shutdown
    assert time.monotonic() - start < 2
    assert_processes_reaped(sleeping_juju.parent)
    if wait_kind == "multi-model":
        assert len(list(sleeping_juju.parent.glob("*.pid"))) == 2


def test_long_operation_uses_enclosing_deadline(sleeping_juju: Path) -> None:
    # GIVEN a long operation, exempt from the ordinary query timeout
    client = ExecutableClient(sleeping_juju, query_timeout=timedelta(milliseconds=1))
    start = time.monotonic()

    # WHEN its enclosing cleanup budget expires
    with timeout_after(0.2), pytest.raises(subprocess.TimeoutExpired) as exc_info:
        client.model(None).cli("kill-controller", "controller", "--timeout", "2m")

    # THEN cleanup cannot outlive that budget or use the shorter query timeout
    assert 0.1 < exc_info.value.timeout <= 0.2
    assert time.monotonic() - start < 2
    assert_processes_reaped(sleeping_juju.parent)


@pytest.mark.parametrize("command", ["bootstrap", "deploy", "run", "exec"])
def test_long_operations_are_not_limited_to_query_budget(tmp_path: Path, command: str) -> None:
    # GIVEN an operation taking longer than the ordinary query limit
    executable = tmp_path / "juju"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(0.1)\nprint('complete')\n")
    executable.chmod(0o755)
    client = ExecutableClient(executable, query_timeout=timedelta(milliseconds=1))

    # WHEN its operation-specific budget permits it to finish
    with timeout_after(2):
        result = client.model(None).cli(command)

    # THEN the ordinary query limit has not truncated the operation
    assert result == "complete\n"


def test_deployment_deadline_includes_deploy_command(sleeping_juju: Path, tmp_path: Path) -> None:
    # GIVEN deployment itself hangs before follow-up polling starts
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text("applications: {}\n")
    backend = JubilantBackend(ExecutableClient(sleeping_juju))

    # WHEN the deployment budget expires
    with pytest.raises(subprocess.TimeoutExpired):
        backend.deploy_bundle_file(
            JujuModelHandle(controller="controller", model="model"),
            str(bundle),
            timeout=timedelta(milliseconds=200),
        )

    # THEN the initial command was bounded, not just its subsequent polling
    assert_processes_reaped(sleeping_juju.parent)


def test_nested_deadlines_cannot_extend_parent_and_restore_after_failure() -> None:
    # GIVEN an enclosing deadline
    with timeout_after(10):
        parent = remaining_timeout(100)

        # WHEN a longer nested budget fails
        with pytest.raises(RuntimeError), timeout_after(100):
            assert remaining_timeout(100) <= parent
            raise RuntimeError("operation failed")

        # THEN the parent remains active, and a shorter nested budget is restored on exit
        with timeout_after(1):
            assert remaining_timeout(100) <= 1
        assert 1 < remaining_timeout(100) <= parent

    assert remaining_timeout(100) == 100
