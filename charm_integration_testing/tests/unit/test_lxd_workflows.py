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
        pytest.param(SetupParams("none", 0), id="successful-registration"),
        pytest.param(SetupParams("parse", 2), id="credential-parsing-fails"),
        pytest.param(SetupParams("add-cloud", 2), id="cloud-registration-fails"),
        pytest.param(SetupParams("add-credential", 2), id="credential-registration-fails"),
    ],
)
def test_lxd_setup_removes_credentials_on_success_and_failure(tmp_path: Path, params: SetupParams) -> None:
    # GIVEN the actual setup script and controllable external command failures
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
    juju.write_text(
        "#!/bin/bash\n"
        'for directory in "$RUNNER_TEMP"/lxd.*; do\n'
        '  test "$(stat -c %a "$directory")" = 700 || exit 3\n'
        "done\n"
        'if [[ "$FAILURE" == "$1" ]]; then exit 2; fi\n'
    )
    for tool in (yq, juju):
        tool.chmod(0o700)
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    # WHEN registration succeeds or any of its external commands fails
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", action["runs"]["steps"][0]["run"]],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "FAILURE": params.failure,
            "RUNNER_TEMP": str(runtime),
            "CLOUD": "test-lxd",
            "LXD_CREDS": yaml.safe_dump(
                {
                    "endpoint": "https://example.invalid:8443",
                    "server-cert": "server",
                    "client-cert": "client",
                    "client-key": "test-only-key",
                }
            ),
        },
        capture_output=True,
        text=True,
    )

    # THEN the failure remains visible and no temporary credential files survive
    assert result.returncode == params.exit_code, result.stderr
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


def test_existing_ps7_proxy_settings_are_preserved() -> None:
    # GIVEN the environment catalog before this PR
    machine_bypass = "127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.internal,.svc.cluster.local"
    k8s_bypass = (
        "127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
        ".canonical.com,.launchpad.net,.internal,.jujucharms.com,.svc.cluster.local"
    )
    current = yaml.safe_load((ROOT / "static/environments.yaml").read_text())["environments"]

    # THEN Kubernetes and OpenStack retain their distinct bootstrap bypass lists
    for name, environment in current.items():
        if name.startswith("ps7-"):
            expected = k8s_bypass if environment["cloud-type"] == "k8s" else machine_bypass
            assert environment["bootstrap-config"]["no-proxy"] == expected


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


def test_cleanup_pins_server_certificate_before_connecting() -> None:
    # GIVEN the cleanup remote-registration script
    workflow = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())
    steps = workflow["jobs"]["cleanup-environment"]["steps"]
    script = next(step["run"] for step in steps if step["name"] == "Set up LXD remote")

    # THEN the pinned certificate is installed before connecting without trust-on-first-use
    assert script.index('/servercerts/charmqa.crt"') < script.index("lxc remote add")
    assert "--accept-certificate" not in script
    cleanup = next(step["run"] for step in steps if step["name"] == "Remove temporary LXD credentials")
    assert '/servercerts/charmqa.crt"' in cleanup
    for step in steps:
        if step["name"] in {
            "Find and delete instances older than age threshold",
            "Find and delete unused Juju networks",
            "Find and delete unused Juju profiles",
        }:
            assert "steps.lxd-remote.outcome == 'success'" in step["if"]
