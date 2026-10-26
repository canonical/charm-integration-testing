# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import subprocess
import sys
from pathlib import Path

import yaml


def test_environment_info_rejects_unknown_cloud_type(tmp_path: Path) -> None:
    # GIVEN the actual environment lookup script and a cloud type it cannot handle
    root = Path(__file__).resolve().parents[3]
    action = yaml.safe_load((root / ".github/actions/get-environment-info/action.yaml").read_text())
    script = action["runs"]["steps"][0]["run"]
    tools = tmp_path / "bin"
    tools.mkdir()
    yq = tools / "yq"
    yq.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "query = sys.argv[1]\n"
        "if query.endswith('.platform'): print('machine')\n"
        "elif query.endswith('.arch'): print('amd64')\n"
        "elif query.endswith('[\"cloud-type\"]'): print('unknown-cloud')\n"
        "else: raise SystemExit(f'unexpected yq query: {query}')\n"
    )
    yq.chmod(0o700)
    table = tmp_path / "static"
    table.mkdir()
    (table / "environments.yaml").touch()
    output = tmp_path / "output"

    # WHEN the lookup reaches cloud-type dispatch
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "WORKSPACE": str(tmp_path),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "ENVIRONMENT_NAME": "test-unknown",
        },
        capture_output=True,
        text=True,
    )

    # THEN it fails explicitly before publishing partial environment outputs
    assert result.returncode == 1
    assert "Unsupported cloud type 'unknown-cloud' for environment 'test-unknown'." in result.stdout
    assert not output.exists()


def test_juju_no_proxy_bypasses_kubernetes_service_domains() -> None:
    environments_path = Path(__file__).resolve().parents[3] / "static" / "environments.yaml"
    environments = yaml.safe_load(environments_path.read_text())["environments"]

    for name, environment in environments.items():
        if environment.get("platform") != "kubernetes":
            continue

        no_proxy = environment.get("model-config", {}).get("juju-no-proxy")
        assert no_proxy is not None, f"{name} does not configure juju-no-proxy"

        no_proxy_domains = no_proxy.split(",")
        assert ".svc" in no_proxy_domains, f"{name} does not bypass .svc domains"
        assert ".svc.cluster.local" in no_proxy_domains, f"{name} does not bypass cluster-local domains"
