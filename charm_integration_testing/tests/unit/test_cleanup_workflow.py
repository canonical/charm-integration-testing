# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


def _step_script(name: str) -> str:
    path = Path(__file__).resolve().parents[3] / ".github/workflows/cleanup-environment.yaml"
    workflow = yaml.safe_load(path.read_text())
    return next(str(step["run"]) for step in workflow["jobs"]["cleanup-environment"]["steps"] if step["name"] == name)


def _run_step(name: str, directory: Path, dry_run: bool) -> subprocess.CompletedProcess[str]:
    stubs = """
    kubectl() { printf 'STUB_DELETE %s\\n' "$*"; }
    openstack() {
      case "$*" in
        "loadbalancer list -f value -c id") ;;
        *" delete "*) printf 'STUB_DELETE %s\\n' "$*" ;;
        *" list"*)
          printf '%s\\n' '[{"ID":"old","id":"old","Name":"old","Status":"DOWN"}]' ;;
        *" show "*)
          printf '%s\\n' '{"created":"2020-01-01T00:00:00Z","created_at":"2020-01-01T00:00:00Z",
            "device_owner":"Octavia","device_id":"lb-gone"}' ;;
        *) echo "Unexpected OpenStack call: $*" >&2; return 1 ;;
      esac
    }
    lxc_stub() {
      case "$1" in
        list) printf '%s\\n' '[{"name":"juju-old","created_at":"2020-01-01T00:00:00Z"}]' ;;
        delete) printf 'STUB_DELETE %s\\n' "$*" ;;
        *) echo "Unexpected LXD call: $*" >&2; return 1 ;;
      esac
    }
    """
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", stubs + _step_script(name)],
        cwd=directory,
        env=os.environ | {"DRY_RUN": str(dry_run).lower(), "AGE_THRESHOLD": "240", "LXC_BINARY": "lxc_stub"},
        text=True,
        capture_output=True,
        timeout=10,
    )


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "delete"])
def test_kubernetes_cleanup_preserves_candidates_without_deleting_in_dry_run(tmp_path: Path, dry_run: bool) -> None:
    # GIVEN both cluster-scoped and namespaced deletion candidates
    candidates = "ClusterRole/juju-credential-fff5a8d9\nqa:ServiceAccount/juju-credential-fff5a8d9\n"
    (tmp_path / "resources_to_remove.txt").write_text(candidates)

    # WHEN the workflow's deletion step runs against a stub client
    result = _run_step("Remove all resources", tmp_path, dry_run)

    # THEN dry-run prints the candidates without calling the client; normal cleanup still deletes both scopes
    assert result.returncode == 0, result.stderr
    if dry_run:
        assert candidates in result.stdout
        assert "STUB_DELETE" not in result.stdout
        assert "rawfile node cleanup skipped" in result.stdout
    else:
        assert "STUB_DELETE delete ClusterRole/juju-credential-fff5a8d9" in result.stdout
        assert "STUB_DELETE delete ServiceAccount/juju-credential-fff5a8d9 --namespace=qa" in result.stdout


def test_kubernetes_dry_run_fails_if_candidates_cannot_be_read(tmp_path: Path) -> None:
    # GIVEN no discovery output file
    # WHEN dry-run tries to report candidates
    result = _run_step("Remove all resources", tmp_path, True)

    # THEN it reports failure rather than a successful empty preview
    assert result.returncode != 0
    assert "resources_to_remove.txt" in result.stderr
    assert "STUB_DELETE" not in result.stdout


def test_cluster_rbac_cleanup_matches_credential_names_and_preserves_age_filter(tmp_path: Path) -> None:
    # GIVEN old singular/plural credentials, QA resources, unrelated names, and a new credential
    names = [
        "juju-credential-fff5a8d9",
        "juju-credentials-AbC12345",
        "charmqa-model",
        "controller-charmqa-test",
        "juju-credential-short",
        "juju-credentials-fff5a8d9-extra",
        "other-juju-credential-fff5a8d9",
        "juju-credential-1234567_",
    ]
    items = [
        {"kind": kind, "metadata": {"name": name, "creationTimestamp": "2020-01-01T00:00:00Z"}}
        for kind in ("ClusterRole", "ClusterRoleBinding")
        for name in names
    ]
    items.append(
        {
            "kind": "ClusterRole",
            "metadata": {"name": "juju-credentials-12345678", "creationTimestamp": "2999-01-01T00:00:00Z"},
        }
    )

    # WHEN the workflow discovers RBAC using the real jq filter and a stub Kubernetes response
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'kubectl() { printf "%s\\n" "$RBAC_FIXTURE"; }\n' + _step_script("Find old QA cluster RBAC"),
        ],
        cwd=tmp_path,
        env=os.environ | {"AGE_THRESHOLD": "240", "RBAC_FIXTURE": json.dumps({"items": items})},
        text=True,
        capture_output=True,
        timeout=10,
    )

    # THEN only old QA resources and exact eight-character singular/plural credential names are selected
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "resources_to_remove.txt").read_text().splitlines() == [
        f"{kind}/{name}" for kind in ("ClusterRole", "ClusterRoleBinding") for name in names[:4]
    ]


@pytest.mark.parametrize(
    "name",
    [
        "Find and delete servers older than age threshold",
        "Find and delete security groups older than age threshold",
        "Find and delete load balancers older than age threshold",
        "Find and delete volumes older than age threshold",
        "Find and delete floating IPs older than age threshold",
        "Find and delete orphaned ports that are in the DOWN state and older than age threshold",
        "Find and delete instances older than age threshold",
    ],
)
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "delete"])
def test_other_substrates_respect_dry_run(tmp_path: Path, name: str, dry_run: bool) -> None:
    # GIVEN clients reporting old deletion candidates
    # WHEN a cleanup step runs in the selected mode
    result = _run_step(name, tmp_path, dry_run)

    # THEN dry-run reports candidates without deletion, while normal cleanup still deletes
    assert result.returncode == 0, result.stderr
    if dry_run:
        assert "Dry run: would delete" in result.stdout
        assert "STUB_DELETE" not in result.stdout
    else:
        assert "STUB_DELETE" in result.stdout
