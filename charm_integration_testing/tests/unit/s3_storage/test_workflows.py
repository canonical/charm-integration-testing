# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import importlib.util
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from s3_storage.configuration import parse_s3_config
from s3_storage_swift.configuration import provider_from_config
from test_suite.fixtures.s3 import can_cleanup_session_storage
from test_suite.scheduler.states import State

ROOT = Path(__file__).resolve().parents[4]
SWIFT_CONFIG_VARIABLES = (
    "S3_OS_AUTH_URL",
    "S3_OS_USERNAME",
    "S3_OS_PASSWORD",
    "S3_OS_USER_DOMAIN_NAME",
    "S3_OS_PROJECT_NAME",
    "S3_OS_PROJECT_DOMAIN_NAME",
    "S3_OS_STORAGE_URL",
    "S3_CLEANUP_ACCESS_KEY",
    "S3_CLEANUP_SECRET_KEY",
    "S3_BUCKET_NAMESPACE",
    "S3_ENDPOINT",
    "S3_REGION",
)
TARGET_SECRET_EXPRESSION = "${{ secrets[steps.target_env_info.outputs.s3-swift-secret] }}"
NEIGHBOR_SECRET_EXPRESSION = "${{ secrets[steps.neighbor_env_info.outputs.s3-swift-secret] }}"


def test_ci_swift_secret_lookup_uses_trusted_environment_metadata() -> None:
    # GIVEN environment metadata as the source of truth for the configured secret name
    workflow = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())
    job = workflow["jobs"]["execute-charm-test"]
    assert "S3_SWIFT_SECRET_NAMES" not in job["env"]
    assert all(not name.startswith("S3_SWIFT_CONFIG_") for name in job["env"])

    # WHEN each environment selects a secret, THEN the workflow dereferences that exact name
    for side in ("target", "neighbor"):
        step = next(step for step in job["steps"] if step.get("id") == f"{side}_s3_config")
        selected = f"steps.{side}_env_info.outputs.s3-swift-secret"
        assert step["env"]["SWIFT_CONFIG"] == f"${{{{ secrets[{selected}] }}}}"
        assert "S3_BUCKET_NAMESPACE" not in step["env"]
        assert "--namespace" not in step["run"]


def test_ci_test_entrypoints_receive_cloud_scoped_config_paths() -> None:
    # GIVEN both CI test entrypoints
    workflow = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())
    steps = workflow["jobs"]["execute-charm-test"]["steps"]
    test_steps = [step for step in steps if step.get("id") in ("run_tests", "run_tests_cmr")]
    # WHEN their runtime environments are inspected
    assert len(test_steps) == 2
    for step in test_steps:
        environment = step["env"]
        # THEN only the path crosses the test-entrypoint boundary
        assert "SWIFT_CONFIG" not in environment
        assert "S3_BUCKET_NAMESPACE" not in environment
        assert environment["S3_CONFIG_target_cloud"] == "${{ steps.target_s3_config.outputs.config_path }}"
    cmr = next(step for step in test_steps if step["id"] == "run_tests_cmr")
    assert cmr["env"]["S3_CONFIG_neighbor_cloud"] == "${{ steps.neighbor_s3_config.outputs.config_path }}"
    assert "steps.setup_target.outputs.cloud-name" in cmr["env"]["neighbor_cloud"]
    assert "inputs.same_controller" in cmr["env"]["neighbor_cloud"]


def test_cleanup_swift_secret_fallback_uses_the_same_fixed_allowlist() -> None:
    # GIVEN cleanup's explicit reusable secret and metadata-based manual fallback
    workflow = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())
    job = workflow["jobs"]["cleanup-environment"]
    tests = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())
    assert json.loads(job["env"]["S3_SWIFT_SECRET_NAMES"]) == json.loads(
        tests["jobs"]["execute-charm-test"]["env"]["S3_SWIFT_SECRET_NAMES"]
    )

    # THEN the fallback cannot select credentials outside the same fixed S3-only set
    step = next(step for step in job["steps"] if step.get("id") == "s3_credentials")
    assert step["env"]["SWIFT_CONFIG"] == (
        "${{ secrets.S3_SWIFT_CONFIG || "
        "(contains(fromJSON(env.S3_SWIFT_SECRET_NAMES), steps.env_info.outputs.s3-swift-secret) "
        "&& secrets[steps.env_info.outputs.s3-swift-secret] || '') }}"
    )


def test_ci_checks_each_environment_swift_secret_before_test_entrypoints() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())
    steps = workflow["jobs"]["execute-charm-test"]["steps"]
    test_steps = [step for step in steps if step.get("id") in ("run_tests", "run_tests_cmr")]
    target_check = next(step for step in steps if step.get("name") == "Check target Swift storage credentials")
    neighbor_check = next(step for step in steps if step.get("name") == "Check neighbor Swift storage credentials")

    assert len(test_steps) == 2
    assert "continue-on-error" not in target_check
    assert target_check["if"] == "${{ steps.target_env_info.outputs.s3-swift-secret != '' }}"
    assert target_check["env"]["SWIFT_SECRET_NAME"] == "${{ steps.target_env_info.outputs.s3-swift-secret }}"
    assert target_check["env"]["SWIFT_CONFIG"] == TARGET_SECRET_EXPRESSION
    assert "scripts/load-swift-config.py" in target_check["run"]
    assert target_check["id"] == "target_s3_config"
    assert '"${RUNNER_TEMP}/target-s3.conf"' in target_check["run"]
    assert neighbor_check["if"] == (
        "${{ inputs.neighbor_environment != '' && steps.neighbor_env_info.outputs.s3-swift-secret != '' }}"
    )
    assert neighbor_check["env"]["SWIFT_SECRET_NAME"] == "${{ steps.neighbor_env_info.outputs.s3-swift-secret }}"
    assert neighbor_check["env"]["SWIFT_CONFIG"] == NEIGHBOR_SECRET_EXPRESSION
    assert neighbor_check["id"] == "neighbor_s3_config"
    assert '"${RUNNER_TEMP}/neighbor-s3.conf"' in neighbor_check["run"]
    test_start = next(step for step in steps if step.get("name") == "Post 'test_start' event")
    assert max(steps.index(target_check), steps.index(neighbor_check)) < min(
        steps.index(test_start), *(steps.index(step) for step in test_steps)
    )


@pytest.mark.parametrize(
    ("secret_name", "prefix", "missing_variable"),
    [
        pytest.param("S3_SWIFT_TEST", "TARGET_", "S3_OS_PASSWORD", id="target-missing"),
        pytest.param("S3_SWIFT_NEIGHBOR", "NEIGHBOR_", "S3_CLEANUP_ACCESS_KEY", id="neighbor-missing"),
        pytest.param("S3_SWIFT_TEST", "TARGET_", "S3_BUCKET_NAMESPACE", id="target-missing-namespace"),
        pytest.param("S3_SWIFT_NEIGHBOR", "NEIGHBOR_", "S3_BUCKET_NAMESPACE", id="neighbor-missing-namespace"),
        pytest.param("S3_SWIFT_TEST", "TARGET_", None, id="target-complete"),
        pytest.param("S3_SWIFT_NEIGHBOR", "NEIGHBOR_", None, id="neighbor-complete"),
    ],
)
def test_ci_preflight_checks_environment_secrets(
    tmp_path: Path, secret_name: str, prefix: str, missing_variable: str | None
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())
    steps = workflow["jobs"]["execute-charm-test"]["steps"]
    check_name = (
        "Check target Swift storage credentials" if prefix == "TARGET_" else "Check neighbor Swift storage credentials"
    )
    preflight = next(step for step in steps if step.get("name") == check_name)
    values = {name: "configured" for name in SWIFT_CONFIG_VARIABLES}
    values["S3_BUCKET_NAMESPACE"] = "shared-storage"
    if missing_variable is not None:
        values[missing_variable] = ""
    config = "\n".join(f"{name}={value}" for name, value in values.items())
    environment = {
        "SWIFT_SECRET_NAME": secret_name,
        "SWIFT_CONFIG": config,
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "RUNNER_TEMP": str(tmp_path),
    }

    result = subprocess.run(
        ["bash", "-c", preflight["run"]],
        capture_output=True,
        check=False,
        env={**os.environ, **environment},
        text=True,
        cwd=ROOT,
    )

    if missing_variable is None:
        assert result.returncode == 0
        path = tmp_path / ("target-s3.conf" if prefix == "TARGET_" else "neighbor-s3.conf")
        assert parse_s3_config(path.read_text(), SWIFT_CONFIG_VARIABLES) == values
        assert path.stat().st_mode & 0o777 == 0o600
        assert Path(environment["GITHUB_OUTPUT"]).read_text() == f"config_path={path}\navailable=true\n"
        assert not Path(environment["GITHUB_ENV"]).exists()
    else:
        assert result.returncode == 1
        assert f"{secret_name} is missing: {missing_variable}" in result.stdout


def test_environment_info_exposes_optional_swift_secret() -> None:
    action = yaml.safe_load((ROOT / ".github/actions/get-environment-info/action.yaml").read_text())
    lookup = action["runs"]["steps"][0]["run"]
    environment_lookups = [line for line in lookup.splitlines() if "yq " in line and ".environments[" in line]

    assert "swift-storage" not in action["outputs"]
    assert action["outputs"]["s3-swift-secret"]["value"] == "${{ steps.lookup.outputs.s3-swift-secret }}"
    assert "s3-bucket-namespace" not in action["outputs"]
    assert any(
        "s3_swift_secret=$(yq -r" in line and "s3-swift-secret" in line and '// ""' in line
        for line in lookup.splitlines()
    )
    assert "swift_storage" not in lookup
    assert 'echo "s3-swift-secret=${s3_swift_secret}"' in lookup
    environments = yaml.safe_load((ROOT / "static/environments.yaml").read_text())["environments"]
    assert all("swift-storage" not in environment for environment in environments.values())
    assert environment_lookups
    assert all("yq -r" in line for line in environment_lookups if " -o=json " not in line)
    assert all("strenv(ENVIRONMENT_NAME)" in line for line in environment_lookups)
    assert all("${ENVIRONMENT_NAME}" not in line for line in environment_lookups)


def test_cleanup_schedule_covers_every_environment_storage_namespace() -> None:
    # GIVEN numbered test environments sharing the same physical substrates
    environments = yaml.safe_load((ROOT / "static/environments.yaml").read_text())["environments"]
    schedule = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment-schedule.yaml").read_text())
    gate_id = "wait-for-previous-sweep"
    gate = schedule["jobs"][gate_id]
    scheduled = [(job_id, job) for job_id, job in schedule["jobs"].items() if job_id != gate_id]

    # WHEN the schedule waits for older sweeps before chaining reusable cleanup jobs
    assert gate["runs-on"] == "ubuntu-latest"
    assert gate["permissions"]["actions"] == "read"
    gate_script = gate["steps"][0]["run"]
    assert "gh run list" in gate_script
    assert "GITHUB_RUN_NUMBER" in gate_script
    assert '.status != "completed"' in gate_script
    assert scheduled[0][1]["needs"] == gate_id
    scheduled_environments = [job["with"]["environment"] for _, job in scheduled]
    scheduled_secret_names = [
        job["secrets"]["S3_SWIFT_CONFIG"].removeprefix("${{ secrets.").removesuffix(" }}") for _, job in scheduled
    ]
    assert len(scheduled_environments) == len(set(scheduled_environments))
    assert len(scheduled_secret_names) == len(set(scheduled_secret_names))

    scheduled_secrets = dict(zip(scheduled_environments, scheduled_secret_names, strict=True))
    expected_secrets = {name: str(environment["s3-swift-secret"]) for name, environment in environments.items()}
    assert len(expected_secrets.values()) == len(set(expected_secrets.values()))
    assert len(scheduled) == len(environments)
    assert scheduled_secrets == expected_secrets
    assert all(job["uses"] == "./.github/workflows/cleanup-environment.yaml" for _, job in scheduled)
    assert all("strategy" not in job for _, job in scheduled)
    for (previous_id, _), (_, job) in zip(scheduled, scheduled[1:], strict=False):
        assert job["needs"] == [gate_id, previous_id]
        assert job["if"] == "${{ !cancelled() && needs.wait-for-previous-sweep.result == 'success' }}"
    # Only the first logical environment for each physical substrate runs broad cleanup.
    seen_substrates: set[tuple[str | None, str | None]] = set()
    for _, job in scheduled:
        name = job["with"]["environment"]
        environment = environments[name]
        substrate = (environment.get("kubeconfig-secret"), environment.get("novarc-secret"))
        assert substrate != (None, None)
        assert job["with"].get("cleanup_substrate", False) is (substrate not in seen_substrates)
        seen_substrates.add(substrate)
    assert len(seen_substrates) == 5
    # THEN every Swift-enabled environment selects its configured secret
    for name, environment in environments.items():
        assert environment.get("swift-storage") is True
        assert environment.get("s3-swift-secret") == expected_secrets[name]
    reusable = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())
    workflow_events = reusable.get("on", reusable.get(True))
    reusable_secrets = workflow_events["workflow_call"]["secrets"]
    cleanup_steps = reusable["jobs"]["cleanup-environment"]["steps"]
    resolve_credentials = next(step for step in cleanup_steps if step.get("name") == "Resolve credentials")
    assert reusable_secrets["S3_SWIFT_CONFIG"]["required"] is False
    assert workflow_events["workflow_call"]["inputs"]["cleanup_substrate"]["default"] is False
    assert workflow_events["workflow_dispatch"]["inputs"]["cleanup_substrate"]["default"] is True
    assert resolve_credentials["if"] == "${{ inputs.cleanup_substrate && success() }}"


def test_cleanup_workflows_serialize_shared_substrate_cleanup() -> None:
    schedule = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment-schedule.yaml").read_text())
    reusable = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())

    assert len(schedule["jobs"]) == 23
    assert reusable["concurrency"] == {
        "group": "charm-qa-environment-cleanup",
        "cancel-in-progress": False,
    }


def test_cleanup_gate_waits_for_older_sweeps_without_a_checkout(tmp_path: Path) -> None:
    # GIVEN a checkout-free runner with older active/pending and newer pending sweeps
    schedule = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment-schedule.yaml").read_text())
    gate_script = schedule["jobs"]["wait-for-previous-sweep"]["steps"][0]["run"]
    calls = tmp_path / "calls"
    sleeps = tmp_path / "sleeps"
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "assert sys.argv[1:3] == ['run', 'list']\n"
        "assert '--repo' in sys.argv, 'No checkout: repository must be explicit'\n"
        "assert sys.argv[sys.argv.index('--repo') + 1] == 'example/test-repository'\n"
        "path = Path(os.environ['GATE_CALLS'])\n"
        "count = int(path.read_text()) if path.exists() else 0\n"
        "path.write_text(str(count + 1))\n"
        "print(json.dumps([\n"
        "    {'number': 18, 'status': 'in_progress' if count == 0 else 'completed'},\n"
        "    {'number': 19, 'status': 'queued' if count == 0 else 'completed'},\n"
        "    {'number': 20, 'status': 'in_progress'},\n"
        "    {'number': 21, 'status': 'queued'},\n"
        "]))\n"
    )
    gh.chmod(0o700)
    sleep = tmp_path / "sleep"
    sleep.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$GATE_SLEEPS"\n')
    sleep.chmod(0o700)

    # WHEN the actual gate script runs outside the repository
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", gate_script],
        capture_output=True,
        check=False,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "example/test-repository",
            "GITHUB_RUN_NUMBER": "20",
            "GITHUB_REF_NAME": "main",
            "GATE_CALLS": str(calls),
            "GATE_SLEEPS": str(sleeps),
        },
    )
    # THEN it waits for both older sweeps, but not itself or newer pending work
    assert result.returncode == 0, result.stderr
    assert calls.read_text() == "2"
    assert sleeps.read_text() == "30\n"
    assert "Waiting for 2 earlier scheduled sweep(s)" in result.stdout


def test_cleanup_gate_failure_blocks_every_scheduled_child(tmp_path: Path) -> None:
    # GIVEN a gate whose GitHub lookup fails before confirming older sweeps have completed
    schedule = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment-schedule.yaml").read_text())
    gate_id = "wait-for-previous-sweep"
    gate_script = schedule["jobs"][gate_id]["steps"][0]["run"]
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o700)
    # WHEN the actual gate script runs under the Actions bash failure settings
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", gate_script],
        capture_output=True,
        check=False,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "example/test-repository",
            "GITHUB_RUN_NUMBER": "20",
            "GITHUB_REF_NAME": "main",
        },
    )
    # THEN no child may bypass the gate, even though cleanup siblings may continue after a failure
    assert result.returncode == 1
    for job_id, job in schedule["jobs"].items():
        if job_id == gate_id:
            continue
        dependencies = job["needs"]
        assert gate_id in ([dependencies] if isinstance(dependencies, str) else dependencies)
        if dependencies != gate_id:
            assert job["if"] == "${{ !cancelled() && needs.wait-for-previous-sweep.result == 'success' }}"


def test_cleanup_imports_storage_from_checkout() -> None:
    # GIVEN the reusable cleanup workflow
    workflow = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    step = next(step for step in steps if step.get("name") == "Remove stale harness S3 buckets")
    install_step = next(step for step in steps if step.get("name") == "Install S3 cleanup dependencies")
    credential_step = next(step for step in steps if step.get("name") == "Check S3 cleanup credentials")
    # THEN the minimal environment can import storage without installing the full suite
    assert step["env"]["PYTHONPATH"] == "${{ github.workspace }}/charm_integration_testing"
    assert "scripts/cleanup-s3.py" in step["run"]
    assert '--config "${S3_CONFIG_PATH}"' in step["run"]
    assert step["env"]["S3_CONFIG_PATH"] == "${{ steps.s3_credentials.outputs.config_path }}"
    assert credential_step["id"] == "s3_credentials"
    assert set(credential_step["env"]) == {"SWIFT_CONFIG", "SWIFT_SECRET_NAME"}
    assert credential_step["env"]["SWIFT_CONFIG"] == (
        "${{ secrets.S3_SWIFT_CONFIG || "
        "(contains(fromJSON(env.S3_SWIFT_SECRET_NAMES), steps.env_info.outputs.s3-swift-secret) "
        "&& secrets[steps.env_info.outputs.s3-swift-secret] || '') }}"
    )
    assert credential_step["env"]["SWIFT_SECRET_NAME"] == "${{ steps.env_info.outputs.s3-swift-secret }}"
    assert "scripts/load-swift-config.py" in credential_step["run"]
    assert "SWIFT_CONFIG" not in step["env"]
    assert credential_step["if"] == (
        "${{ !cancelled() && steps.env_info.outcome == 'success' && "
        "steps.env_info.outputs.swift-storage == 'true' }}"
    )
    assert install_step["id"] == "s3_dependencies"
    assert install_step["if"] == (
        "${{ !cancelled() && steps.s3_credentials.outcome == 'success' && "
        "steps.s3_credentials.outputs.available == 'true' }}"
    )
    assert step["if"] == "${{ !cancelled() && steps.s3_dependencies.outcome == 'success' }}"


@pytest.mark.parametrize("missing_variable", ["all", "S3_OS_PASSWORD", None], ids=["absent", "partial", "complete"])
def test_cleanup_requires_declared_swift_credentials(tmp_path: Path, missing_variable: str | None) -> None:
    # GIVEN cleanup for an environment declaring Swift storage
    workflow = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())
    steps = workflow["jobs"]["cleanup-environment"]["steps"]
    preflight = next(step for step in steps if step.get("id") == "s3_credentials")
    values = {name: "configured" for name in SWIFT_CONFIG_VARIABLES}
    if missing_variable is not None and missing_variable in values:
        del values[missing_variable]
    config = "" if missing_variable == "all" else "\n".join(f"{name}={value}" for name, value in values.items())
    output = tmp_path / "github-output"

    # WHEN the actual workflow preflight runs, THEN missing credentials fail instead of reporting unavailable
    result = subprocess.run(
        ["bash", "-c", preflight["run"]],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "SWIFT_SECRET_NAME": "S3_SWIFT_TEST",
            "SWIFT_CONFIG": config,
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(tmp_path),
        },
        text=True,
        cwd=ROOT,
    )

    if missing_variable is None:
        assert result.returncode == 0
        config_path = tmp_path / "cleanup-s3.conf"
        assert config_path.stat().st_mode & 0o777 == 0o600
        assert output.read_text() == f"config_path={config_path}\navailable=true\n"
    else:
        assert result.returncode == 1
        assert "::error::S3_SWIFT_TEST is missing:" in result.stdout
        assert "configured" not in result.stdout
        assert not output.exists()
        assert not (tmp_path / "cleanup-s3.conf").exists()


def test_cleanup_entrypoint_passes_config_and_age_to_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location("cleanup_s3", ROOT / "scripts/cleanup-s3.py")
    assert spec is not None and spec.loader is not None
    cleanup_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleanup_script)
    assert cleanup_script.provider_from_config is provider_from_config

    class SessionStub:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    class ProviderStub:
        def __init__(self) -> None:
            self.age: timedelta | None = None

        def cleanup_stale(self, age: timedelta) -> list[str]:
            self.age = age
            return ["stale-bucket"]

    session = SessionStub()
    provider = ProviderStub()
    observed: dict[str, Any] = {}

    def provider_factory(path: Path, used_session: SessionStub) -> ProviderStub:
        observed["config"] = path
        observed["session"] = used_session
        return provider

    monkeypatch.setattr(sys, "argv", ["cleanup-s3.py", "--config", "/test/storage.conf", "--age-minutes", "17.5"])
    monkeypatch.setattr(cleanup_script.requests, "Session", lambda: session)
    monkeypatch.setattr(cleanup_script, "provider_from_config", provider_factory)

    cleanup_script.main()

    assert observed == {"config": Path("/test/storage.conf"), "session": session}
    assert provider.age == timedelta(minutes=17.5)
    assert capsys.readouterr().out == "Removed S3 bucket stale-bucket\n"


def test_cleanup_entrypoint_provider_errors_exit_nonzero() -> None:
    runner = """
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("cleanup_s3", "scripts/cleanup-s3.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class SessionStub:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

class FailingProviderStub:
    def cleanup_stale(self, age):
        raise RuntimeError("cleanup failed")

module.requests.Session = SessionStub
module.provider_from_config = lambda path, session: FailingProviderStub()
sys.argv = ["cleanup-s3.py", "--config", "/test/storage.conf"]
module.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", runner],
        capture_output=True,
        check=False,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "charm_integration_testing")},
        text=True,
    )

    assert result.returncode == 1
    assert "cleanup failed" in result.stderr


@pytest.mark.parametrize(
    ("config", "message"),
    [
        pytest.param("OS_PASSWORD=unrecognized", "Invalid SWIFT_CONFIG entry on line 1", id="unknown-variable"),
        pytest.param(
            "S3_OS_PASSWORD=first\nS3_OS_PASSWORD=second",
            "Duplicate SWIFT_CONFIG entry for S3_OS_PASSWORD",
            id="duplicate-variable",
        ),
    ],
)
def test_swift_config_loader_rejects_invalid_entries(tmp_path: Path, config: str, message: str) -> None:
    environment = {
        **os.environ,
        "SWIFT_CONFIG": config,
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
    }
    result = subprocess.run(
        [
            "python3",
            "scripts/load-swift-config.py",
            "S3_SWIFT_TEST",
            "--output-file",
            str(tmp_path / "s3.conf"),
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 1
    assert message in result.stdout


def test_optional_swift_config_skips_when_missing(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    result = subprocess.run(
        [
            "python3",
            "scripts/load-swift-config.py",
            "S3_SWIFT_TEST",
            "--optional",
            "--output-file",
            str(tmp_path / "s3.conf"),
        ],
        capture_output=True,
        check=False,
        env={**os.environ, "SWIFT_CONFIG": "", "GITHUB_OUTPUT": str(output)},
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0
    assert output.read_text() == "available=false\n"
    assert not (tmp_path / "s3.conf").exists()


def test_optional_swift_config_rejects_partial_values(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "python3",
            "scripts/load-swift-config.py",
            "S3_SWIFT_TEST",
            "--optional",
            "--output-file",
            str(tmp_path / "s3.conf"),
        ],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "SWIFT_CONFIG": "S3_OS_AUTH_URL=https://identity.example/v3",
            "GITHUB_ENV": str(tmp_path / "github-env"),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        },
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 1
    assert "S3_SWIFT_TEST is missing: " in result.stdout
    assert "S3_OS_PASSWORD" in result.stdout


@pytest.mark.parametrize("variable", SWIFT_CONFIG_VARIABLES[:-2])
@pytest.mark.parametrize("optional", [False, True], ids=["required", "optional"])
def test_swift_config_loader_rejects_whitespace_only_required_values(
    tmp_path: Path, variable: str, optional: bool
) -> None:
    # GIVEN a declared configuration with one required value containing only whitespace
    values = {name: "configured" for name in SWIFT_CONFIG_VARIABLES}
    values[variable] = " \t "
    config_path = tmp_path / "s3.conf"
    output = tmp_path / "github-output"

    # WHEN the loader runs, THEN no protected configuration or success output is created
    result = subprocess.run(
        [
            sys.executable,
            "scripts/load-swift-config.py",
            "S3_SWIFT_TEST",
            "--output-file",
            str(config_path),
            *(["--optional"] if optional else []),
        ],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "SWIFT_CONFIG": "\n".join(f"{name}={value}" for name, value in values.items()),
            "GITHUB_OUTPUT": str(output),
        },
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 1
    assert f"S3_SWIFT_TEST is missing: {variable}" in result.stdout
    assert not config_path.exists()
    assert not output.exists()


def test_swift_config_loader_writes_optional_endpoint_and_region(tmp_path: Path) -> None:
    environment_file = tmp_path / "github-env"
    values = {name: "configured" for name in SWIFT_CONFIG_VARIABLES}
    values["S3_ENDPOINT"] = "https://s3.example"
    values["S3_REGION"] = "region-1"
    config = "\n".join(f"{name}={value}" for name, value in values.items())
    result = subprocess.run(
        [
            "python3",
            "scripts/load-swift-config.py",
            "S3_SWIFT_TEST",
            "--output-file",
            str(tmp_path / "s3.conf"),
        ],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "SWIFT_CONFIG": config,
            "GITHUB_ENV": str(environment_file),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        },
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0
    config_values = parse_s3_config((tmp_path / "s3.conf").read_text(), SWIFT_CONFIG_VARIABLES)
    assert config_values["S3_ENDPOINT"] == "https://s3.example"
    assert config_values["S3_REGION"] == "region-1"
    assert not environment_file.exists()


def test_swift_config_loader_preserves_equals_and_masks_values(tmp_path: Path) -> None:
    environment_file = tmp_path / "github-env"
    output_file = tmp_path / "github-output"
    values = {name: "configured" for name in SWIFT_CONFIG_VARIABLES}
    values["S3_OS_PASSWORD"] = " first=second%value "
    values["S3_ENDPOINT"] = "https://s3.example"
    config = "# config comment\n" + "\n".join(f"{name}={value}" for name, value in values.items())
    result = subprocess.run(
        [
            "python3",
            "scripts/load-swift-config.py",
            "S3_SWIFT_NEIGHBOR",
            "--output-file",
            str(tmp_path / "s3.conf"),
            "--optional",
        ],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "SWIFT_CONFIG": config,
            "GITHUB_ENV": str(environment_file),
            "GITHUB_OUTPUT": str(output_file),
        },
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0
    assert "first=second%25value" in result.stdout
    assert parse_s3_config((tmp_path / "s3.conf").read_text(), SWIFT_CONFIG_VARIABLES) == values
    assert not environment_file.exists()
    assert output_file.read_text() == f"config_path={tmp_path / 's3.conf'}\navailable=true\n"


def test_swift_config_loader_does_not_overwrite_existing_credentials(tmp_path: Path) -> None:
    # GIVEN an existing file that could belong to another cloud
    path = tmp_path / "existing.conf"
    path.write_text("existing configuration\n")
    values = {name: "configured" for name in SWIFT_CONFIG_VARIABLES}
    # WHEN the loader is given the same destination, THEN it fails without replacing it
    result = subprocess.run(
        [
            "python3",
            "scripts/load-swift-config.py",
            "S3_SWIFT_TEST",
            "--output-file",
            str(path),
        ],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "SWIFT_CONFIG": "\n".join(f"{name}={value}" for name, value in values.items()),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        },
        text=True,
        cwd=ROOT,
    )
    assert result.returncode != 0
    assert path.read_text() == "existing configuration\n"
    assert not (tmp_path / "github-output").exists()


@pytest.mark.parametrize(
    ("current_state", "expected"),
    [
        pytest.param(State.NO_CONTROLLER, True, id="new-controller"),
        pytest.param(State.DEPLOYED, False, id="resumed-controller"),
    ],
)
def test_session_s3_cleanup_only_runs_when_this_run_owns_controller(current_state: State, expected: bool) -> None:
    assert can_cleanup_session_storage(current_state) is expected
