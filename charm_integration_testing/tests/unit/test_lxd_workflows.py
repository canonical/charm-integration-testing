# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class SetupParams:
    failure: str
    exit_code: int


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(SetupParams("none", 0), id="successful-preparation"),
        pytest.param(SetupParams("parse", 2), id="credential-parsing-fails"),
    ],
)
def test_lxd_setup_prepares_cloud_files_for_job_lifetime(tmp_path: Path, params: SetupParams) -> None:
    # GIVEN the actual setup script; Juju registration belongs to the test fixture
    action = yaml.safe_load((ROOT / ".github/actions/setup-environment/setup-lxd/action.yaml").read_text())
    tools = tmp_path / "bin"
    tools.mkdir()
    yq = tools / "yq"
    yq.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, yaml\n"
        "if os.environ['FAILURE'] == 'parse': sys.exit(2)\n"
        "with open(sys.argv[2]) as source: print(yaml.safe_load(source)[sys.argv[1][1:]])\n"
    )
    juju = tools / "juju"
    juju.write_text("#!/bin/bash\n" "echo 'Unexpected Juju registration in setup action' >&2\n" "exit 3\n")
    for tool in (yq, juju):
        tool.chmod(0o700)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    workspace = runtime / "juju-cloud.test"
    workspace.mkdir(mode=0o700)
    output = tmp_path / "output"
    environment = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "FAILURE": params.failure,
        "RUNNER_TEMP": str(runtime),
        "CREDENTIALS_DIR": str(workspace),
        "GITHUB_OUTPUT": str(output),
        "CLOUD": "test-lxd",
        "LXD_CREDS": yaml.safe_dump(
            {
                "endpoint": "https://example.invalid:8443",
                "server-cert": "server",
                "client-cert": "client",
                "client-key": "test-only-key",
            }
        ),
    }

    # WHEN file preparation succeeds or credential parsing fails
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", action["runs"]["steps"][0]["run"]],
        env=environment,
        capture_output=True,
        text=True,
    )

    # THEN failures clean immediately, while successful preparation exposes usable private files
    assert result.returncode == params.exit_code, result.stderr
    assert not (workspace / "creds.yaml").exists()
    if params.exit_code == 0:
        assert workspace.stat().st_mode & 0o777 == 0o700
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
        cloud_path = Path(outputs["cloud-definition-path"])
        credentials_path = Path(outputs["credentials-path"])
        assert yaml.safe_load(cloud_path.read_text())["clouds"]["test-lxd"] == {
            "type": "lxd",
            "auth-types": ["certificate"],
            "endpoint": "https://example.invalid:8443",
            "regions": {"default": {"endpoint": "https://example.invalid:8443"}},
        }
        assert yaml.safe_load(credentials_path.read_text())["credentials"]["test-lxd"]["test-lxd-cert"] == {
            "auth-type": "certificate",
            "server-cert": "server\n",
            "client-cert": "client\n",
            "client-key": "test-only-key\n",
        }
        for path in (cloud_path, credentials_path):
            assert path.stat().st_mode & 0o777 == 0o600

        assert set(workspace.iterdir()) == {cloud_path, credentials_path}
    else:
        assert list(runtime.iterdir()) == []


def test_cloud_dispatch_passes_only_supported_inputs() -> None:
    # GIVEN every local setup action invoked by the dispatcher
    dispatcher = yaml.safe_load((ROOT / ".github/actions/setup-environment/setup-cloud/action.yaml").read_text())

    # WHEN comparing the forwarded inputs with each action's contract
    for step in dispatcher["runs"]["steps"]:
        if "uses" not in step:
            continue
        action = yaml.safe_load((ROOT / step["uses"] / "action.yaml").read_text())
        supplied = step.get("with", {})

        # THEN no required input is missing and no unsupported input is passed
        assert supplied.keys() <= action["inputs"].keys()
        for name, definition in action["inputs"].items():
            if definition.get("required"):
                assert name in supplied


def test_lxd_cleanup_preserves_potentially_active_networks_and_profiles() -> None:
    # GIVEN cleanup running independently of test jobs on a shared LXD cluster
    workflow = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())
    steps = {step["name"]: step for step in workflow["jobs"]["cleanup-environment"]["steps"]}

    # THEN only age-filtered instances are deleted; empty usage is not proof of stale ownership
    assert steps["Find and delete instances older than age threshold"]["if"] == (
        "${{ !cancelled() && steps.lxd-remote.outcome == 'success' }}"
    )
    for step in steps.values():
        script = step.get("run", "")
        assert "lxc network delete" not in script
        assert "lxc profile delete" not in script


def test_lxd_cloud_paths_reach_same_controller_registration() -> None:
    # GIVEN the LXD action outputs and dispatcher
    action = yaml.safe_load((ROOT / ".github/actions/setup-environment/setup-lxd/action.yaml").read_text())
    dispatcher = yaml.safe_load((ROOT / ".github/actions/setup-environment/setup-cloud/action.yaml").read_text())
    workflow = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())

    # THEN both paths are exposed through to the CMR test environment
    for output in ("cloud-definition-path", "credentials-path"):
        assert output in action["outputs"]
        assert f"steps.setup-lxd.outputs.{output}" in dispatcher["outputs"][output]["value"]
    cmr = next(
        step for job in workflow["jobs"].values() for step in job["steps"] if step["name"] == "Run charm CMR tests"
    )
    assert cmr["env"]["CLOUD_DEFINITION_neighbor_cloud"] == "${{ steps.setup_neighbor.outputs.cloud-definition-path }}"
    assert cmr["env"]["CLOUD_CREDENTIALS_neighbor_cloud"] == "${{ steps.setup_neighbor.outputs.credentials-path }}"


@dataclass(frozen=True)
class TimestampParams:
    timestamp: str
    exit_code: int
    deleted: bool
    delete_exit_code: int = 0


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(TimestampParams("2020-01-01T00:00:00Z", 0, True), id="whole-seconds"),
        pytest.param(TimestampParams("2020-01-01T00:00:00.123456789Z", 0, True), id="fractional-seconds"),
        pytest.param(TimestampParams("2999-01-01T00:00:00.123Z", 0, False), id="too-young"),
        pytest.param(TimestampParams("invalid", 5, False), id="invalid-timestamp-fails"),
        pytest.param(
            TimestampParams("2020-01-01T00:00:00Z", 9, True, delete_exit_code=9),
            id="delete-failure-propagates",
        ),
    ],
)
def test_lxd_cleanup_parses_timestamps(tmp_path: Path, params: TimestampParams) -> None:
    # GIVEN the actual cleanup script and an LXD CLI boundary returning a timestamp
    workflow = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["cleanup-environment"]["steps"]
        if step["name"] == "Find and delete instances older than age threshold"
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    lxc = tools / "lxc"
    lxc.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps([{'name': 'juju-test-0', 'created_at': os.environ['TIMESTAMP']}]))\n"
        "elif sys.argv[1] == 'delete':\n"
        "    pathlib.Path(os.environ['DELETED']).touch()\n"
        "    sys.exit(int(os.environ.get('DELETE_EXIT_CODE', '0')))\n"
        "else: sys.exit(2)\n"
    )
    lxc.chmod(0o700)
    deleted = tmp_path / "deleted"

    # WHEN filtering old instances
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "LXC_BINARY": str(lxc),
            "TIMESTAMP": params.timestamp,
            "AGE_THRESHOLD": "240",
            "DELETED": str(deleted),
            "DELETE_EXIT_CODE": str(params.delete_exit_code),
        },
        capture_output=True,
        text=True,
    )

    # THEN fractional dates work and invalid dates fail without deleting anything
    assert result.returncode == params.exit_code, result.stderr
    assert deleted.exists() == params.deleted


def test_ps7_proxy_bypasses_only_private_destinations() -> None:
    # GIVEN the shared PS7 proxy policy
    bypass = "127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.internal,.svc,.svc.cluster.local"
    proxy = "http://egress.ps7.internal:3128"
    current = yaml.safe_load((ROOT / "static/environments.yaml").read_text())["environments"]

    # THEN public endpoints such as streams.canonical.com use the proxy on every PS7 substrate
    for name, environment in current.items():
        if name.startswith("ps7-"):
            bootstrap = environment["bootstrap-config"]
            model = environment["model-config"]
            assert bootstrap["no-proxy"] == model["juju-no-proxy"] == bypass
            for key in ("http-proxy", "https-proxy", "apt-http-proxy", "apt-https-proxy"):
                assert bootstrap[key] == proxy
            for key in ("juju-http-proxy", "juju-https-proxy", "snap-http-proxy", "snap-https-proxy"):
                assert model[key] == proxy


def test_lxd_runner_matches_workflow_placement() -> None:
    # GIVEN LXD environments and the existing private-endpoint workflow jobs
    environments = yaml.safe_load((ROOT / "static/environments.yaml").read_text())["environments"]
    runners = yaml.safe_load((ROOT / "static/github-runners.yaml").read_text())["github-runners"]
    workflows = [
        yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        for name in ("charm-testing.yaml", "cleanup-environment.yaml")
    ]

    # THEN LXD metadata selects the runner labels actually used by both workflows
    for environment in environments.values():
        if environment["cloud-type"] == "lxd":
            labels = runners[environment["github-runner"]]["labels"]
            for workflow in workflows:
                assert any(job.get("runs-on") == labels for job in workflow["jobs"].values())


def test_cleanup_lxd_remote_uses_isolated_config() -> None:
    # GIVEN the cleanup remote setup script
    workflow = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())
    steps = workflow["jobs"]["cleanup-environment"]["steps"]
    script = next(step["run"] for step in steps if step["name"] == "Set up LXD remote")

    # THEN the direct snap binary loads the isolated config with supplied credentials
    assert '/servercerts/charmqa.crt"' in script
    assert '"${LXD_CONF}/client.crt"' in script
    assert '"${LXD_CONF}/client.key"' in script
    assert "auth_type: tls" in script
    assert 'LXC_BINARY="/snap/lxd/current/bin/lxc"' in script
    assert '"$LXC_BINARY" remote list --format json | jq -e \'has("charmqa")\'' in script
    cleanup = next(step["run"] for step in steps if step["name"] == "Remove temporary LXD credentials")
    assert '/servercerts/charmqa.crt"' in cleanup
    for step in steps:
        if step["name"] in {
            "Find and delete instances older than age threshold",
            "Find and delete unused Juju networks",
            "Find and delete unused Juju profiles",
        }:
            assert "steps.lxd-remote.outcome == 'success'" in step["if"]
    delete_instances = next(
        step["run"] for step in steps if step["name"] == "Find and delete instances older than age threshold"
    )
    assert '"$LXC_BINARY" delete --force "charmqa:${name}"' in delete_instances
