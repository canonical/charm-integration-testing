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

args = sys.argv[1:]
context = ""
if "-i" in args:
    prompt = args[args.index("-i") + 1]
    path = prompt.removeprefix("Please read ").removesuffix(
        " for project context, then await my instructions."
    )
    context = pathlib.Path(path).read_text()
pathlib.Path(os.environ["COPILOT_CAPTURE"]).write_text(json.dumps({"args": args, "context": context}))
sys.exit(int(os.environ.get("COPILOT_EXIT", "0")))
"""
    )
    copilot.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "COPILOT_CAPTURE": str(tmp_path / "capture.json"),
    }


def test_autonomous_agent_preserves_prompt_arguments_and_cleans_up(
    tmp_path: Path, agent_environment: dict[str, str]
) -> None:
    # GIVEN a task with shell metacharacters and a local MCP configuration
    config = tmp_path / "mcp config.json"
    config.write_text("{}")
    agent_environment["SANDBOX_MCP_CONFIG_FILE"] = str(config)
    task = "literal $(touch should-not-exist); 'quoted'"

    # WHEN the agent task invokes Copilot
    result = subprocess.run(
        [str(AGENT_TASK), task, "--", "--resume", "session id"],
        cwd=tmp_path,
        env=agent_environment,
        capture_output=True,
        text=True,
        check=False,
    )

    # THEN Copilot receives literal arguments and the temporary prompt is removed
    assert result.returncode == 0, result.stderr
    args = json.loads((tmp_path / "capture.json").read_text())["args"]
    assert args[:3] == ["--yolo", "--additional-mcp-config", f"@{config}"]
    assert args[-2:] == ["--resume", "session id"]
    assert f"Task: {task}" in args[args.index("-p") + 1]
    assert not (tmp_path / "should-not-exist").exists()
    assert not list(tmp_path.glob("tmp.*"))


def test_interactive_agent_supplies_context_and_propagates_failure(
    tmp_path: Path, agent_environment: dict[str, str]
) -> None:
    # GIVEN a failing Copilot invocation
    agent_environment["COPILOT_EXIT"] = "23"

    # WHEN interactive mode is invoked with a task and passthrough argument
    result = subprocess.run(
        [str(AGENT_TASK), "--interactive", "a task", "--", "--continue"],
        env=agent_environment,
        capture_output=True,
        text=True,
        check=False,
    )

    # THEN Copilot can read the context, its exit code is retained, and the prompt is removed
    assert result.returncode == 23
    capture = json.loads((tmp_path / "capture.json").read_text())
    assert capture["args"][0:2] == ["--yolo", "-i"]
    assert capture["args"][-1] == "--continue"
    assert "Task: a task" in capture["context"]
    assert not list(tmp_path.glob("tmp.*"))


def test_agent_rejects_missing_task_or_mcp_config(tmp_path: Path, agent_environment: dict[str, str]) -> None:
    # GIVEN no task, or a missing MCP configuration
    no_task = subprocess.run([str(AGENT_TASK)], env=agent_environment, capture_output=True, text=True, check=False)
    agent_environment["SANDBOX_MCP_CONFIG_FILE"] = str(tmp_path / "missing.json")
    no_config = subprocess.run(
        [str(AGENT_TASK), "task"], env=agent_environment, capture_output=True, text=True, check=False
    )

    # THEN neither invocation starts Copilot
    assert no_task.returncode != 0
    assert "Usage:" in no_task.stderr
    assert no_config.returncode != 0
    assert "SANDBOX_MCP_CONFIG_FILE not found" in no_config.stderr
    assert not (tmp_path / "capture.json").exists()
