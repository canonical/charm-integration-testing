# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
AGENT_TASK = PROJECT_ROOT / "mise-tasks" / "agent"


@pytest.fixture
def agent_environment(tmp_path: Path) -> dict[str, str]:
    copilot = tmp_path / "copilot"
    copilot.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

pathlib.Path(os.environ["COPILOT_CAPTURE"]).write_text(json.dumps({
    "args": sys.argv[1:],
    "cwd": os.getcwd(),
    "copilot_home": os.environ.get("COPILOT_HOME"),
}))
if "--acp" in sys.argv:
    print(json.dumps({"jsonrpc": "2.0", "id": 0, "result": {"ready": True}}), flush=True)
sys.exit(int(os.environ.get("COPILOT_EXIT", "0")))
"""
    )
    copilot.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "COPILOT_CAPTURE": str(tmp_path / "capture.json"),
        "COPILOT_HOME": str(tmp_path / "copilot-home"),
    }


def run_agent(args: list[str], agent_environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(AGENT_TASK), *args],
        cwd=PROJECT_ROOT.parent,
        env=agent_environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_autonomous_mode_preserves_task_mcp_and_copilot_arguments(
    tmp_path: Path, agent_environment: dict[str, str]
) -> None:
    # GIVEN a task, MCP config and native Copilot session arguments
    config = tmp_path / "mcp config.json"
    config.write_text("{}")
    agent_environment["SANDBOX_MCP_CONFIG_FILE"] = str(config)
    task = "literal $(touch should-not-exist); 'quoted'"

    # WHEN autonomous mode runs
    result = run_agent([task, "--", "--resume", "session id"], agent_environment)

    # THEN task text is literal, arguments are passed through, and temporary files are removed
    assert result.returncode == 0, result.stderr
    capture = json.loads((tmp_path / "capture.json").read_text())
    assert capture["args"][:3] == ["--yolo", "--additional-mcp-config", f"@{config}"]
    assert f"Task: {task}" in capture["args"][capture["args"].index("-p") + 1]
    assert capture["args"][-2:] == ["--resume", "session id"]
    assert capture["copilot_home"] == str(tmp_path / "copilot-home")
    assert capture["cwd"] == str(PROJECT_ROOT)
    assert not (tmp_path / "should-not-exist").exists()
    assert not list(tmp_path.glob("tmp.*"))


def test_interactive_mode_keeps_yolo_and_argument_passthrough(
    tmp_path: Path, agent_environment: dict[str, str]
) -> None:
    # WHEN interactive mode receives a task and Copilot arguments
    result = run_agent(["--interactive", "a task", "--", "--continue"], agent_environment)

    # THEN the existing interactive invocation and temporary-file cleanup are preserved
    assert result.returncode == 0, result.stderr
    capture = json.loads((tmp_path / "capture.json").read_text())
    assert capture["args"][0] == "--yolo"
    assert capture["args"][1] == "-i"
    assert "Task: a task" in capture["args"][2]
    assert capture["args"][-1] == "--continue"
    assert not list(tmp_path.glob("tmp.*"))


def test_acp_mode_keeps_stdio_clean_and_uses_repository_context(
    tmp_path: Path, agent_environment: dict[str, str]
) -> None:
    # WHEN ACP starts with MCP config and an explicitly selected Copilot home
    config = tmp_path / "mcp.json"
    config.write_text("{}")
    agent_environment["SANDBOX_MCP_CONFIG_FILE"] = str(config)
    result = run_agent(["--acp", "--", "--model", "auto"], agent_environment)

    # THEN ACP gets unmodified protocol stdout, no legacy prompt or permission flags, and the selected checkout
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"jsonrpc": "2.0", "id": 0, "result": {"ready": True}}
    assert result.stderr == ""
    capture = json.loads((tmp_path / "capture.json").read_text())
    assert capture["args"] == ["--acp", "--additional-mcp-config", f"@{config}", "--model", "auto"]
    assert capture["copilot_home"] == str(tmp_path / "copilot-home")
    assert capture["cwd"] == str(PROJECT_ROOT)
    assert (PROJECT_ROOT / "AGENTS.md").read_text().strip() == "@.agents/skills/system.md"
    assert not list(tmp_path.glob("tmp.*"))


def test_mise_runs_agent_with_direct_stdio() -> None:
    # WHEN Mise resolves the agent task
    result = subprocess.run(
        ["mise", "tasks", "info", "agent", "--json"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    # THEN Mise connects the task directly to stdin, stdout and stderr
    assert json.loads(result.stdout)["raw"] is True


@pytest.mark.parametrize(
    "args",
    [
        ["--acp", "task"],
        ["--acp", "--interactive"],
        ["--acp", "--", "--prompt", "task"],
        ["--acp", "--", "-p", "task"],
        ["--acp", "--", "-ptask"],
        ["--acp", "--", "--interactive"],
        ["--acp", "--", "-i"],
        ["--", "--acp"],
    ],
)
def test_incompatible_modes_fail_before_starting_copilot(
    tmp_path: Path, agent_environment: dict[str, str], args: list[str]
) -> None:
    # WHEN ACP is combined with another invocation mode or supplied as a forwarded flag
    result = run_agent(args, agent_environment)

    # THEN the invalid invocation is diagnosed without starting Copilot
    assert result.returncode != 0
    assert "ERROR:" in result.stderr
    assert not (tmp_path / "capture.json").exists()


@pytest.mark.parametrize("args", [["a task"], ["--acp"]])
def test_modes_propagate_copilot_failure(tmp_path: Path, agent_environment: dict[str, str], args: list[str]) -> None:
    # GIVEN Copilot exits unsuccessfully
    agent_environment["COPILOT_EXIT"] = "23"

    # WHEN either legacy autonomous or ACP mode runs
    result = run_agent(args, agent_environment)

    # THEN the same exit status reaches the caller
    assert result.returncode == 23
