# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
ACTIONS = ROOT / ".github" / "actions"


def action(path: str) -> dict[str, Any]:
    result = yaml.safe_load((ACTIONS / path / "action.yaml").read_text())
    assert isinstance(result, dict)
    return result


def step_script(path: str, step_id: str) -> str:
    script = next(step["run"] for step in action(path)["runs"]["steps"] if step.get("id") == step_id)
    assert isinstance(script, str)
    return script


def run_step(script: str, tmp_path: Path, **variables: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "WORKSPACE": str(ROOT),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            **variables,
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(shutil.which("yq") is None, reason="requires mikefarah/yq")
@pytest.mark.parametrize(
    ("environment", "cloud_type", "platform"),
    [
        ("github-runner-k8s", "local-k8s", "kubernetes"),
        ("github-runner-lxd", "local-lxd", "machine"),
    ],
)
def test_local_environment_lookup(tmp_path: Path, environment: str, cloud_type: str, platform: str) -> None:
    # WHEN a hosted environment is looked up
    result = run_step(step_script("get-environment-info", "lookup"), tmp_path, ENVIRONMENT_NAME=environment)

    # THEN it has no private credentials and reports the expected substrate
    assert result.returncode == 0, result.stderr
    outputs = (tmp_path / "output").read_text().splitlines()
    assert f"cloud-type={cloud_type}" in outputs
    assert f"platform={platform}" in outputs
    assert "credentials-secret=" in outputs
    assert "s3-secret-name=" in outputs


@pytest.mark.skipif(shutil.which("yq") is None, reason="requires mikefarah/yq")
@pytest.mark.parametrize(
    ("neighbor", "expected_status"),
    [("", 0), ("github-runner-lxd", 0), ("ps7-sqa-prod_charm_qa_lxd-1", 1)],
)
def test_runner_selection_rejects_mixed_runner_types(tmp_path: Path, neighbor: str, expected_status: int) -> None:
    # WHEN selecting a runner for local and optional neighbor environments
    result = run_step(
        step_script("pick-runner", "pick"),
        tmp_path,
        TARGET_ENV="github-runner-k8s",
        NEIGHBOR_ENV=neighbor,
    )

    # THEN compatible environments select the hosted runner, and incompatible ones fail
    assert result.returncode == expected_status, result.stderr
    if expected_status:
        assert "Runner type mismatch" in result.stdout
    else:
        assert 'runner-labels=["ubuntu-24.04"]' in (tmp_path / "output").read_text()


@pytest.mark.parametrize(("cloud_type", "task"), [("local-k8s", "setup-k8s"), ("local-lxd", "setup-lxd")])
def test_local_cloud_dispatch(tmp_path: Path, cloud_type: str, task: str) -> None:
    # GIVEN a fake Mise executable and the selected local cloud step
    mise = tmp_path / "mise"
    mise.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
with pathlib.Path(os.environ["CALLS"]).open("a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
"""
    )
    mise.chmod(0o755)
    step_id = f"setup-{cloud_type}"
    calls_file = tmp_path / "calls"

    # WHEN the setup step runs
    result = run_step(
        step_script("setup-environment/setup-cloud", step_id),
        tmp_path,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        CLOUD_NAME="target-cloud",
        CLOUD_WORKSPACE=str(tmp_path / "cloud"),
        CALLS=str(calls_file),
    )

    # THEN the dispatcher uses the shared setup group and substrate task
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert calls[0] == ["run", "setup", "--", "--group", cloud_type]
    assert calls[1][:3] == ["run", task, "--"]
    assert calls[1][-2:] == ["--output", str(tmp_path / "output")]
    if cloud_type == "local-k8s":
        assert calls[1][3:7] == ["--cloud-name", "target-cloud", "--kubeconfig", str(tmp_path / "cloud/kubeconfig")]


def test_local_cloud_outputs_and_cleanup_guard() -> None:
    # GIVEN local task output keys and the ephemeral cleanup workflow
    cloud = action("setup-environment/setup-cloud")
    cleanup = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())

    # THEN dispatcher outputs use the local task results and cleanup rejects local clouds
    assert "steps.setup-local-lxd.outputs.cloud-name" in cloud["outputs"]["cloud-name"]["value"]
    assert "steps.setup-local-k8s.outputs.kubeconfig-path" in cloud["outputs"]["kubeconfig_path"]["value"]
    steps = next(iter(cleanup["jobs"].values()))["steps"]
    reject = next(step for step in steps if step.get("name") == "Reject local runner environments")
    assert "startsWith(steps.env_info.outputs.cloud-type, 'local-')" in reject["if"]
    assert "exit 1" in reject["run"]
