# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_TOOLS = {"python", "uv", "pypi:poetry"}


@dataclass
class SetupRunner:
    environment: dict[str, str]
    log: Path

    def run(self, *arguments: str, **variables: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(ROOT / "mise-tasks/setup"), *arguments],
            env={**self.environment, **variables},
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


@pytest.fixture
def setup_runner(tmp_path: Path) -> SetupRunner:
    binary = tmp_path / "mise"
    binary.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys\n"
        "with open(os.environ['COMMAND_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == os.environ.get('FAIL_COMMAND'):\n"
        "    sys.exit(37)\n"
    )
    binary.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("SETUP_")}
    environment.update(PATH=f"{tmp_path}:/usr/bin:/bin", COMMAND_LOG=str(log))
    return SetupRunner(environment, log)


def test_default_setup_only_prepares_python(setup_runner: SetupRunner) -> None:
    # GIVEN a fresh setup invocation
    # WHEN no additional selection is requested
    result = setup_runner.run()

    # THEN Python tooling and project dependencies are installed, without host packages
    assert result.returncode == 0, result.stderr
    calls = setup_runner.calls()
    assert set(calls[0][2:]) == PYTHON_TOOLS
    assert calls[0][:2] == ["install", "--locked"]
    assert calls[1:] == [["exec", "--", "poetry", "install"]]


def test_composed_setup_preserves_poetry_arguments(setup_runner: SetupRunner) -> None:
    # GIVEN two selections sharing Python tooling
    # WHEN Poetry arguments follow the selection separator
    result = setup_runner.run("development", "integration", "--", "--only", "main")

    # THEN a single named install precedes project reconciliation
    assert result.returncode == 0, result.stderr
    calls = setup_runner.calls()
    tools = set(calls[0][2:])
    assert PYTHON_TOOLS | {"node", "npm:markdownlint-cli2", "aqua:trufflesecurity/trufflehog"} <= tools
    assert "aqua:helm/helm" not in tools
    assert "pypi:pipx" not in tools
    assert not any(tool.startswith("pipx:") for tool in tools)
    assert calls[1:] == [["exec", "--", "poetry", "install", "--only", "main"]]


def test_local_k8s_setup_is_independent_of_python(setup_runner: SetupRunner) -> None:
    # GIVEN a host needing local Kubernetes tooling but not project dependencies
    # WHEN only local-k8s is selected
    result = setup_runner.run("--tools-only", "local-k8s")

    # THEN only Kubernetes executables and named host packages are requested
    assert result.returncode == 0, result.stderr
    assert setup_runner.calls() == [
        ["install", "--locked", "aqua:helm/helm", "aqua:jqlang/jq"],
        ["run", "bootstrap-packages", "--", "snap:k8s", "snap:kubectl", "snap:juju"],
    ]


@pytest.mark.parametrize(
    "arguments,variables",
    [
        (("development", "unknown"), {}),
        (("--bootstrap",), {}),
        (("--tools-only", "python", "--", "--only", "main"), {}),
        ((), {"SETUP_INSTALL_PROJECT": ""}),
        ((), {"SETUP_TOOLS": "development\nINJECTED=true"}),
    ],
)
def test_invalid_setup_fails_before_mutation(
    setup_runner: SetupRunner, arguments: tuple[str, ...], variables: dict[str, str]
) -> None:
    # GIVEN invalid setup inputs
    # WHEN setup validates the complete request
    result = setup_runner.run(*arguments, **variables)

    # THEN no tool or host package installation has begun
    assert result.returncode != 0
    assert result.stderr
    assert setup_runner.calls() == []


@pytest.mark.parametrize("command", ["install", "run"])
def test_setup_failure_stops_project_installation(setup_runner: SetupRunner, command: str) -> None:
    # GIVEN a failed executable or host package installation
    # WHEN setup prepares a local substrate
    result = setup_runner.run("local-k8s", FAIL_COMMAND=command)

    # THEN the original failure propagates without attempting Poetry installation
    assert result.returncode == 37
    assert not any(call[0] == "exec" for call in setup_runner.calls())


def test_action_selection_skips_implicit_python(setup_runner: SetupRunner) -> None:
    # GIVEN an action requesting workflow tools without project installation
    # WHEN environment inputs reach the shared setup script
    result = setup_runner.run(SETUP_TOOLS="workflow", SETUP_INSTALL_PROJECT="false")

    # THEN only the requested workflow clients are prepared
    assert result.returncode == 0, result.stderr
    assert setup_runner.calls() == [["install", "--locked", "aqua:jqlang/jq", "aqua:mikefarah/yq", "aqua:cli/cli"]]
