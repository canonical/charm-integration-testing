# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for sandbox shell command execution."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[3]


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """Provide an isolated Multipass substitute and a VM checkout."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mount = tmp_path / "project"
    mount.mkdir()
    log = tmp_path / "calls.jsonl"

    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)
    multipass = bin_dir / "multipass"
    multipass.write_text(
        """#!/usr/bin/env python3
import json
import os
import subprocess
import sys

args = sys.argv[1:]
with open(os.environ["SANDBOX_TEST_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[0] == "info":
    print(json.dumps({"info": {args[1]: {"state": "Running", "mounts": {
        os.environ["SANDBOX_MOUNT"]: {}
    }}}}))
    sys.exit(0)
if args[0] != "exec" or args[2] != "--":
    sys.exit(1)
command = args[3:]
if command[:2] == ["bash", "-lc"] and "gh auth status" in command[2]:
    sys.exit(0)
if command[:2] == ["bash", "-c"] and any("exec bash -l" in arg for arg in command):
    sys.exit(0)
sys.exit(subprocess.run(command).returncode)
"""
    )
    multipass.chmod(0o755)
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("SANDBOX_") and name not in {"GH_TOKEN", "GITHUB_TOKEN"}
    }
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        SANDBOX_VM="test-vm",
        SANDBOX_MOUNT=str(mount),
        SANDBOX_TEST_LOG=str(log),
        SANDBOX_VAR_TEST_VALUE="value with spaces",
    )
    return env, mount, log


@pytest.mark.parametrize("launcher", ["script", "mise"])
def test_shell_passes_command_arguments_and_environment(
    sandbox: tuple[dict[str, str], Path, Path], launcher: str
) -> None:
    env, mount, _ = sandbox
    command = [
        "/usr/bin/python3",
        "-c",
        "import json, os, sys; print(json.dumps([sys.argv[1:], os.getcwd(), "
        "os.environ['PROJECT_ROOT'], os.environ['UV_PROJECT_ENVIRONMENT'], "
        "os.environ['TEST_VALUE'], sys.stdin.read()]))",
        "a b",
        "",
        "$(not-a-command)",
    ]
    prefix = (
        ["bash", str(PROJECT / "scripts/sandbox.sh"), "shell", "--"]
        if launcher == "script"
        else ["mise", "run", "sandbox", "shell", "--"]
    )

    result = subprocess.run(
        [*prefix, *command], cwd=PROJECT, env=env, input="stdin text\n", capture_output=True, text=True, check=True
    )

    args, cwd, project_root, uv_env, value, stdin = json.loads(result.stdout.splitlines()[-1])
    assert args == ["a b", "", "$(not-a-command)"]
    assert cwd == project_root == str(mount.resolve())
    digest = hashlib.sha256(str(mount.resolve()).encode()).hexdigest()
    assert uv_env.endswith(f"/charm-integration-testing/venvs/{digest}")
    assert value == "value with spaces"
    assert stdin == "stdin text\n"


def test_shell_returns_command_failure(sandbox: tuple[dict[str, str], Path, Path]) -> None:
    env, _, _ = sandbox

    result = subprocess.run(
        ["bash", str(PROJECT / "scripts/sandbox.sh"), "shell", "--", "/bin/sh", "-c", "exit 37"],
        cwd=PROJECT,
        env=env,
        input="",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 37


def test_shell_without_command_remains_interactive(sandbox: tuple[dict[str, str], Path, Path]) -> None:
    env, _, log = sandbox

    subprocess.run(
        ["bash", str(PROJECT / "scripts/sandbox.sh"), "shell"],
        cwd=PROJECT,
        env=env,
        input="",
        capture_output=True,
        text=True,
        check=True,
    )

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert any("exec bash -l" in arg for call in calls for arg in call)
