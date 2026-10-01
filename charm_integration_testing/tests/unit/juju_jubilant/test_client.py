# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import subprocess

import jubilant
import pytest
from juju import JujuModelHandle
from juju_jubilant.client import JubilantClient

MODEL = JujuModelHandle(controller="controller", model="model", owner="owner")


def test_ssh_bounds_process_and_preserves_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(
        args: list[str], *, check: bool, capture_output: bool, text: bool, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        assert args == ["juju", "ssh", "--model", MODEL.uri, "1", "cat /proc/sys/kernel/random/boot_id"]
        assert check and capture_output and text
        assert timeout == 7.5
        return subprocess.CompletedProcess(args, 0, stdout="boot-id\n", stderr="")

    monkeypatch.setattr("juju_jubilant.client.subprocess.run", run)
    result = JubilantClient().ssh(MODEL, "1", "cat /proc/sys/kernel/random/boot_id", timeout=7.5)
    assert result == "boot-id\n"


@pytest.mark.parametrize("timed_out", [False, True])
def test_ssh_preserves_errors(monkeypatch: pytest.MonkeyPatch, timed_out: bool) -> None:
    def run(
        args: list[str], *, check: bool, capture_output: bool, text: bool, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if timed_out:
            raise subprocess.TimeoutExpired(args, timeout)
        raise subprocess.CalledProcessError(255, args, output="", stderr="connection refused")

    monkeypatch.setattr("juju_jubilant.client.subprocess.run", run)
    with pytest.raises(subprocess.TimeoutExpired if timed_out else jubilant.CLIError):
        JubilantClient().ssh(MODEL, "1", "command", timeout=1)
