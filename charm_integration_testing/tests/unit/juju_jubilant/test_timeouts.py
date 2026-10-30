# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import jubilant
import pytest
from juju import JujuModelHandle
from juju_jubilant import JubilantBackend
from juju_jubilant.client import JubilantClient


class ExecutableClient(JubilantClient):
    def __init__(self, executable: Path, command_timeout: timedelta = timedelta(minutes=5)) -> None:
        super().__init__(command_timeout)
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


@pytest.mark.parametrize("command", ["status", "bootstrap", "deploy", "run", "exec", "ssh", "scp"])
def test_command_timeout_kills_and_reaps_cli(sleeping_juju: Path, command: str) -> None:
    # GIVEN a Juju command that never responds within its command budget
    client = ExecutableClient(sleeping_juju, command_timeout=timedelta(milliseconds=200))

    # WHEN the query runs
    with pytest.raises(subprocess.TimeoutExpired):
        client.model(None).cli(command)

    # THEN the timed-out CLI is not left running
    assert_processes_reaped(sleeping_juju.parent)


def test_unit_queries_use_bounded_transport(sleeping_juju: Path) -> None:
    # GIVEN a unit query backed by a blocked Juju status command
    backend = JubilantBackend(ExecutableClient(sleeping_juju, command_timeout=timedelta(milliseconds=200)))

    # WHEN the backend fetches status
    with pytest.raises(subprocess.TimeoutExpired):
        backend.num_units(JujuModelHandle(controller="controller", model="model"), "application")

    # THEN the timed-out CLI is not left running
    assert_processes_reaped(sleeping_juju.parent)


def test_unit_queries_preserve_cli_error_details(tmp_path: Path) -> None:
    # GIVEN a command that fails with useful output
    executable = tmp_path / "juju"
    executable.write_text(f"#!{sys.executable}\nimport sys\nprint('unreachable', file=sys.stderr)\nsys.exit(1)\n")
    executable.chmod(0o755)
    backend = JubilantBackend(ExecutableClient(executable))

    # WHEN the status query fails
    with pytest.raises(jubilant.CLIError) as exc_info:
        backend.num_units(JujuModelHandle(controller="controller", model="model"), "application")

    # THEN the native Jubilant exception retains the command's diagnostics
    assert exc_info.value.returncode == 1
    assert exc_info.value.stderr == "unreachable\n"


def test_controller_cleanup_bounds_subprocess_not_just_juju_wait() -> None:
    # GIVEN a controller removal that would block during API connection setup
    backend = JubilantBackend()
    error = subprocess.TimeoutExpired(cmd="juju kill-controller", timeout=300)

    # WHEN cleanup runs through the public backend operation
    with patch("jubilant._juju.subprocess.run", side_effect=error) as run:
        with pytest.raises(subprocess.TimeoutExpired):
            backend.kill_controller("controller")

    # THEN a subprocess limit covers connection setup as well as Juju's own wait
    assert run.call_args.kwargs["timeout"] == 300


@pytest.mark.parametrize("wait_kind", ["single-model", "multi-model", "model-exists"])
def test_polling_uses_command_timeout(sleeping_juju: Path, wait_kind: str) -> None:
    # GIVEN blocked status queries with a short per-command limit
    backend = JubilantBackend(ExecutableClient(sleeping_juju, command_timeout=timedelta(milliseconds=200)))
    models = [JujuModelHandle(controller="controller", model=name) for name in ("first", "second")]
    budget = timedelta(seconds=10)
    start = time.monotonic()

    # WHEN a status query blocks, including concurrent multi-model queries
    with pytest.raises(subprocess.TimeoutExpired):
        if wait_kind == "multi-model":
            backend.wait_idle_multi_model(models, timeout=budget, count=1, strict_timeout=True)
        elif wait_kind == "single-model":
            backend.wait_idle(models[0], timeout=budget, count=1, strict_timeout=True)
        else:
            backend.wait_for_model_to_exist(models[0], timeout=budget)

    # THEN command timeouts also bound executor shutdown, without a shared deadline
    assert time.monotonic() - start < 2
    assert_processes_reaped(sleeping_juju.parent)
    if wait_kind == "multi-model":
        assert len(list(sleeping_juju.parent.glob("*.pid"))) == 2


@pytest.mark.parametrize(("explicit_timeout", "expected"), [(2, 2), (600, 300)])
def test_explicit_timeout_respects_command_ceiling(explicit_timeout: float, expected: float) -> None:
    # GIVEN an explicit subprocess timeout supplied by Jubilant
    client = JubilantClient().model(None)
    error = subprocess.TimeoutExpired(cmd="juju status", timeout=expected)

    # WHEN the command runs, THEN shorter limits survive and longer ones are capped
    with patch("jubilant._juju.subprocess.run", side_effect=error) as run:
        with pytest.raises(subprocess.TimeoutExpired):
            client._cli("status", timeout=explicit_timeout)

    assert run.call_args.kwargs["timeout"] == expected
