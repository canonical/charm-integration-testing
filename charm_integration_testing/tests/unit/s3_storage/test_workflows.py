# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import importlib.util
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


def test_cleanup_swift_secret_lookup_uses_environment_metadata() -> None:
    # GIVEN cleanup's metadata-based lookup for manual and scheduled runs
    workflow = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())
    job = workflow["jobs"]["cleanup-environment"]
    # THEN cleanup uses the exact metadata-selected secret without a second name registry
    assert "S3_SWIFT_SECRET_NAMES" not in job["env"]
    step = next(step for step in job["steps"] if step.get("id") == "s3_credentials")
    assert step["env"]["SWIFT_CONFIG"] == "${{ secrets[steps.env_info.outputs.s3-swift-secret] }}"


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
    assert credential_step["env"]["SWIFT_CONFIG"] == "${{ secrets[steps.env_info.outputs.s3-swift-secret] }}"
    assert credential_step["env"]["SWIFT_SECRET_NAME"] == "${{ steps.env_info.outputs.s3-swift-secret }}"
    assert "scripts/load-swift-config.py" in credential_step["run"]
    assert "SWIFT_CONFIG" not in step["env"]
    assert credential_step["if"] == (
        "${{ !cancelled() && steps.age_threshold.outcome == 'success' && "
        "steps.env_info.outcome == 'success' && steps.env_info.outputs.s3-swift-secret != '' }}"
    )
    assert install_step["id"] == "s3_dependencies"
    assert install_step["if"] == (
        "${{ !cancelled() && steps.age_threshold.outcome == 'success' && "
        "steps.s3_credentials.outcome == 'success' && "
        "steps.s3_credentials.outputs.available == 'true' }}"
    )
    assert step["if"] == (
        "${{ !cancelled() && steps.age_threshold.outcome == 'success' && "
        "steps.s3_dependencies.outcome == 'success' }}"
    )


def test_cleanup_schedule_preserves_one_environment_per_shared_substrate() -> None:
    # GIVEN the original five scheduled environments rather than every logical alias
    expected = [
        "ps6-sqa-stg_charm_qa_kubernetes-1",
        "ps6-sqa-prod_charm_qa_kubernetes-1",
        "ps6-sqa-prod_charm_qa_openstack-1",
        "ps7-sqa-prod_charm_qa_kubernetes-1",
        "ps7-sqa-prod_charm_qa_openstack-1",
    ]
    schedule = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment-schedule.yaml").read_text())
    environments = yaml.safe_load((ROOT / "static/environments.yaml").read_text())["environments"]
    reusable = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())

    # WHEN inspecting the matrix, THEN each substrate is scheduled once and secrets are inherited
    assert set(schedule["jobs"]) == {"cleanup-schedule"}
    job = schedule["jobs"]["cleanup-schedule"]
    assert job["strategy"]["matrix"] == {"environment": expected}
    assert len(expected) == len(set(expected))
    assert job["strategy"]["fail-fast"] is False
    assert job["uses"] == "./.github/workflows/cleanup-environment.yaml"
    assert job["with"] == {"environment": "${{ matrix.environment }}"}
    events = reusable.get("on", reusable.get(True))
    assert job["secrets"] == "inherit"
    assert all(not name.startswith("S3_") for name in events["workflow_call"]["secrets"])
    substrates = set()
    swift_secrets = []
    for name in expected:
        info = environments[name]
        substrates.add((info.get("kubeconfig-secret"), info.get("novarc-secret")))
        swift_secrets.append(info["s3-swift-secret"])
    assert len(substrates) == len(expected)
    assert all(swift_secrets)
    assert len(swift_secrets) == len(set(swift_secrets))
    assert events["workflow_dispatch"]["inputs"]["environment"]["options"] == expected
    assert "cleanup_substrate" not in events["workflow_call"]["inputs"]
    assert "cleanup_substrate" not in events["workflow_dispatch"]["inputs"]


def test_cleanup_preserves_per_environment_cancellation() -> None:
    # GIVEN independent cleanup runs for each representative environment
    reusable = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())

    # THEN only an older run for the same workflow/environment is cancelled
    assert reusable["concurrency"] == {
        "group": "${{ github.workflow }}-${{ inputs.environment }}",
        "cancel-in-progress": True,
    }


@pytest.mark.parametrize(
    ("threshold", "expected_status"),
    [
        pytest.param("1.5", 0, id="positive-fraction"),
        pytest.param("0", 1, id="zero"),
        pytest.param("-1", 1, id="negative"),
        pytest.param("nan", 1, id="nan"),
        pytest.param("inf", 1, id="infinity"),
    ],
)
def test_cleanup_validates_finite_positive_threshold(tmp_path: Path, threshold: str, expected_status: int) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())
    steps = workflow["jobs"]["cleanup-environment"]["steps"]
    validator = next(step for step in steps if step.get("id") == "age_threshold")
    guarded = [step for step in steps if "!cancelled()" in step.get("if", "")]

    assert steps.index(validator) < min(steps.index(step) for step in guarded)
    assert all("steps.age_threshold.outcome == 'success'" in step["if"] for step in guarded)

    result = subprocess.run(
        ["bash", "-c", validator["run"]],
        capture_output=True,
        check=False,
        env={**os.environ, "AGE_THRESHOLD": threshold},
        text=True,
        cwd=ROOT,
    )

    assert result.returncode == expected_status
    if expected_status:
        assert "age_threshold must be a positive finite number" in result.stdout


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
            self.pending_cleanup_calls = 0

        def cleanup_stale(self, age: timedelta) -> list[str]:
            self.age = age
            return ["stale-bucket"]

        def cleanup_pending_resources(self) -> None:
            self.pending_cleanup_calls += 1

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
    assert provider.pending_cleanup_calls == 1
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
    def cleanup_pending_resources(self):
        pass

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
