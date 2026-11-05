# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Exercise sandbox context and skill loading without a VM or authentication."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copyfile(Path(__file__).parents[1] / "sandbox.sh", project / "scripts/sandbox.sh")
    (project / ".agents/skills").mkdir(parents=True)
    (project / ".agents/skills/system.md").write_text("Test context\n")
    (project / "development-sandbox").mkdir()
    (tmp_path / "vm-project").symlink_to(project, target_is_directory=True)
    tools = tmp_path / "bin"
    tools.mkdir()
    stubs = {
        "gh": "#!/bin/sh\nexit 0\n",
        "multipass": """#!/usr/bin/env python3
import json
import os
import subprocess
import sys

if sys.argv[1] == "info":
    print(json.dumps({"info": {"test-vm": {
        "state": "Running", "mounts": {os.environ["SANDBOX_MOUNT"]: {}}
    }}}))
else:
    command = sys.argv[sys.argv.index("--") + 1:]
    # Keep the fake tools on PATH instead of loading the host's login profile.
    if command[:2] == ["bash", "-lc"]:
        command[1] = "-c"
    sys.exit(subprocess.run(command, check=False).returncode)
""",
        "copilot": """#!/usr/bin/env python3
import json
import pathlib
import sys

pathlib.Path("copilot-args.json").write_text(json.dumps(sys.argv[1:]))
args = sys.argv[1:]
if "-p" in args:
    prompt = args[args.index("-p") + 1]
else:
    prompt_file = args[args.index("-i") + 1].split()[2]
    prompt = pathlib.Path(prompt_file).read_text()
pathlib.Path("copilot-prompt.txt").write_text(prompt)
""",
    }
    for name, content in stubs.items():
        path = tools / name
        path.write_text(content)
        path.chmod(0o755)
    return project


def run_sandbox(
    project: Path,
    config_dir: str | None,
    interactive: bool = False,
    prompt_file: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{project.parent / 'bin'}:{os.defpath}",
        "HOME": str(project.parent),
        "SANDBOX_VM": "test-vm",
        "SANDBOX_MOUNT": str(project.parent / "vm-project"),
        "SANDBOX_VAR_GITHUB_TOKEN": "unused",
        "SANDBOX_VAR_COPILOT_GITHUB_TOKEN": "unused",
    }
    if config_dir is not None:
        env["SANDBOX_EXTRA_CONFIG_DIR"] = config_dir
    if prompt_file is not None:
        env["SANDBOX_PROMPT_FILE"] = prompt_file
    args = ["--interactive"] if interactive else ["test task"]
    return subprocess.run(
        ["bash", str(project / "scripts/sandbox.sh"), "run", *args],
        cwd=project.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


@pytest.mark.parametrize("interactive", [False, True], ids=["autonomous", "interactive"])
@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_extra_skills_reach_copilot(sandbox: Path, interactive: bool, absolute: bool) -> None:
    name = "custom skills ' $literal"
    config = sandbox / name
    (config / ".github/skills/example").mkdir(parents=True)
    result = run_sandbox(sandbox, str(config) if absolute else name, interactive)
    assert result.returncode == 0, result.stderr
    args = json.loads((sandbox / "copilot-args.json").read_text())
    assert args[args.index("--add-dir") + 1] == str(sandbox.parent / "vm-project" / name)
    assert ("-i" if interactive else "-p") in args


def test_default_does_not_add_directory(sandbox: Path) -> None:
    result = run_sandbox(sandbox, None)
    assert result.returncode == 0, result.stderr
    assert "--add-dir" not in json.loads((sandbox / "copilot-args.json").read_text())
    assert (sandbox / "copilot-prompt.txt").read_text() == "Test context\n\n\n---\n\nTask: test task"


@pytest.mark.parametrize("kind", ["missing", "no-skills", "outside", "symlink-outside"])
def test_invalid_config_fails_before_copilot(sandbox: Path, kind: str) -> None:
    config = sandbox / "custom"
    if kind == "no-skills":
        config.mkdir()
    elif kind in {"outside", "symlink-outside"}:
        outside = sandbox.parent / "outside"
        (outside / ".github/skills").mkdir(parents=True)
        if kind == "outside":
            config = outside
        else:
            config.symlink_to(outside, target_is_directory=True)
    result = run_sandbox(sandbox, str(config))
    assert result.returncode != 0
    assert "ERROR: SANDBOX_EXTRA_CONFIG_DIR" in result.stderr
    assert not (sandbox / "copilot-args.json").exists()


@pytest.mark.parametrize("interactive", [False, True], ids=["autonomous", "interactive"])
@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute-host-path"])
def test_custom_prompt_replaces_default(sandbox: Path, interactive: bool, absolute: bool) -> None:
    # GIVEN a custom context, optionally outside the mounted checkout
    directory = sandbox.parent if absolute else sandbox
    prompt = directory / "custom context ' $literal.md"
    prompt.write_text("Custom context\n")
    (sandbox / "custom/.github/skills").mkdir(parents=True)

    # WHEN a run combines custom context and additional skills
    result = run_sandbox(sandbox, "custom", interactive, str(prompt) if absolute else prompt.name)

    # THEN only the custom context and optional task reach Copilot
    assert result.returncode == 0, result.stderr
    suffix = "" if interactive else "\n\n---\n\nTask: test task"
    assert (sandbox / "copilot-prompt.txt").read_text() == f"Custom context\n{suffix}"


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_invalid_prompt_fails_before_copilot(sandbox: Path, kind: str) -> None:
    prompt = sandbox / "invalid"
    if kind == "directory":
        prompt.mkdir()
    result = run_sandbox(sandbox, None, prompt_file=str(prompt))
    assert result.returncode != 0
    assert "ERROR: Sandbox prompt file" in result.stderr
    assert not (sandbox / "copilot-args.json").exists()
