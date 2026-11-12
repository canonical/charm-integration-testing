# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import subprocess
from pathlib import Path


def _task_environment(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = tmp_path / "commands"
    stubs = {
        "concierge": "exit 0",
        "juju": 'echo "{}"',
        "jq": "exit 0",
        "sudo": """
case "$1" in
    */concierge) exit 0 ;;
    iptables) printf '%s\\n' "$*" >> "$COMMANDS" ;;
    k8s) if [ "${FAIL_CONFIG:-}" = 1 ]; then exit 1; fi; echo kubeconfig ;;
    *) exit 1 ;;
esac
""",
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "COMMANDS": str(commands)}
    task = Path(__file__).resolve().parents[3] / "mise-tasks" / "setup-k8s"
    return task, env, commands


def test_setup_k8s_matches_concierge_forwarding_and_secures_kubeconfig(tmp_path: Path) -> None:
    task, env, commands = _task_environment(tmp_path)
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("old")
    kubeconfig.chmod(0o644)
    for _ in range(2):
        subprocess.run(
            ["bash", str(task), "--no-chaos-mesh", "--kubeconfig", str(kubeconfig)],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    assert commands.read_text().splitlines() == [
        "iptables -F FORWARD",
        "iptables -P FORWARD ACCEPT",
        "iptables -F FORWARD",
        "iptables -P FORWARD ACCEPT",
    ]
    assert kubeconfig.read_text() == "kubeconfig\n"
    assert kubeconfig.stat().st_mode & 0o777 == 0o600


def test_setup_k8s_preserves_existing_kubeconfig_if_export_fails(tmp_path: Path) -> None:
    task, env, _ = _task_environment(tmp_path)
    env["FAIL_CONFIG"] = "1"
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("existing")

    result = subprocess.run(
        ["bash", str(task), "--no-chaos-mesh", "--kubeconfig", str(kubeconfig)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert kubeconfig.read_text() == "existing"
    assert list(tmp_path.glob("kubeconfig.*")) == []
