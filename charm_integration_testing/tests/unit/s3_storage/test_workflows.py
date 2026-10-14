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
    "S3_ENDPOINT",
    "S3_REGION",
)
TARGET_SECRET_EXPRESSION = "${{ secrets[steps.target_env_info.outputs.s3-swift-secret] }}"
NEIGHBOR_SECRET_EXPRESSION = "${{ secrets[steps.neighbor_env_info.outputs.s3-swift-secret] }}"


def test_ci_test_entrypoints_inherit_target_environment_credentials() -> None:
    # GIVEN both CI test entrypoints
    workflow = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())
    steps = workflow["jobs"]["execute-charm-test"]["steps"]
    test_steps = [step for step in steps if step.get("id") in ("run_tests", "run_tests_cmr")]
    # WHEN their runtime environments are inspected
    assert len(test_steps) == 2
    for step in test_steps:
        environment = step["env"]
        # THEN each test entrypoint inherits credentials loaded by the preceding preflight step
        assert "SWIFT_CONFIG" not in environment
        assert environment["S3_BUCKET_NAMESPACE"] == ("${{ steps.target_env_info.outputs.s3-swift-secret }}")


def test_ci_checks_each_environment_swift_secret_before_test_entrypoints() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())
    steps = workflow["jobs"]["execute-charm-test"]["steps"]
    test_steps = [step for step in steps if step.get("id") in ("run_tests", "run_tests_cmr")]
    target_check = next(step for step in steps if step.get("name") == "Check target Swift storage credentials")
    neighbor_check = next(step for step in steps if step.get("name") == "Check neighbor Swift storage credentials")

    assert len(test_steps) == 2
    assert "continue-on-error" not in target_check
    assert target_check["if"] == "${{ steps.target_env_info.outputs.swift-storage == 'true' }}"
    assert target_check["env"]["SWIFT_SECRET_NAME"] == "${{ steps.target_env_info.outputs.s3-swift-secret }}"
    assert target_check["env"]["SWIFT_CONFIG"] == TARGET_SECRET_EXPRESSION
    assert "scripts/load-swift-config.py" in target_check["run"]
    assert "--prefix TARGET_" in target_check["run"]
    assert neighbor_check["if"] == (
        "${{ inputs.neighbor_environment != '' && steps.neighbor_env_info.outputs.swift-storage == 'true' }}"
    )
    assert neighbor_check["env"]["SWIFT_SECRET_NAME"] == "${{ steps.neighbor_env_info.outputs.s3-swift-secret }}"
    assert neighbor_check["env"]["SWIFT_CONFIG"] == NEIGHBOR_SECRET_EXPRESSION
    assert "--prefix NEIGHBOR_" in neighbor_check["run"]
    assert max(steps.index(target_check), steps.index(neighbor_check)) < min(steps.index(step) for step in test_steps)


@pytest.mark.parametrize(
    ("secret_name", "prefix", "missing_variable"),
    [
        pytest.param("S3_SWIFT_TEST", "TARGET_", "S3_OS_PASSWORD", id="target-missing"),
        pytest.param("S3_SWIFT_NEIGHBOR", "NEIGHBOR_", "S3_CLEANUP_ACCESS_KEY", id="neighbor-missing"),
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
    if missing_variable is not None:
        values[missing_variable] = ""
    config = "\n".join(f"{name}={value}" for name, value in values.items())
    environment = {
        "SWIFT_SECRET_NAME": secret_name,
        "SWIFT_CONFIG": config,
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
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
        output = Path(environment["GITHUB_ENV"]).read_text()
        assert output.splitlines() == [
            *(f"{prefix}{name}=configured" for name in SWIFT_CONFIG_VARIABLES),
            f"{prefix}S3_BUCKET_NAMESPACE={secret_name}",
        ]
    else:
        assert result.returncode == 1
        assert f"{secret_name} is missing: {missing_variable}" in result.stdout


def test_environment_info_exposes_swift_storage_flag() -> None:
    action = yaml.safe_load((ROOT / ".github/actions/get-environment-info/action.yaml").read_text())
    lookup = action["runs"]["steps"][0]["run"]

    assert action["outputs"]["swift-storage"]["value"] == "${{ steps.lookup.outputs.swift-storage }}"
    assert action["outputs"]["s3-swift-secret"]["value"] == "${{ steps.lookup.outputs.s3-swift-secret }}"
    assert any(
        "swift_storage=$(yq -r" in line and "swift-storage" in line and "// false" in line
        for line in lookup.splitlines()
    )
    assert "must set s3-swift-secret" in lookup
    assert 'echo "swift-storage=${swift_storage}"' in lookup


def test_cleanup_schedule_covers_every_environment_storage_namespace() -> None:
    # GIVEN numbered test environments sharing the same physical substrates
    environments = yaml.safe_load((ROOT / "static/environments.yaml").read_text())["environments"]
    schedule = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment-schedule.yaml").read_text())
    scheduled = list(schedule["jobs"].items())

    # WHEN the schedule chains reusable cleanup jobs to avoid dropping matrix entries
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
    assert "needs" not in scheduled[0][1]
    for (previous_id, _), (_, job) in zip(scheduled, scheduled[1:], strict=False):
        assert job["needs"] == previous_id
        assert job["if"] == "${{ !cancelled() }}"
    # THEN every Swift-enabled environment selects its configured secret
    for name, environment in environments.items():
        assert environment.get("swift-storage") is True
        assert environment.get("s3-swift-secret") == expected_secrets[name]
    reusable = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())
    workflow_events = reusable.get("on", reusable.get(True))
    reusable_secrets = workflow_events["workflow_call"]["secrets"]
    assert reusable_secrets["S3_SWIFT_CONFIG"]["required"] is False


def test_cleanup_workflows_serialize_shared_substrate_cleanup() -> None:
    schedule = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment-schedule.yaml").read_text())
    reusable = yaml.safe_load((ROOT / ".github/workflows/cleanup-environment.yaml").read_text())

    assert len(schedule["jobs"]) == 22
    assert reusable["concurrency"] == {
        "group": "charm-qa-environment-cleanup",
        "cancel-in-progress": False,
    }


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
    assert credential_step["id"] == "s3_credentials"
    assert set(credential_step["env"]) == {"SWIFT_CONFIG", "SWIFT_SECRET_NAME"}
    assert credential_step["env"]["SWIFT_CONFIG"] == (
        "${{ secrets.S3_SWIFT_CONFIG || secrets[steps.env_info.outputs.s3-swift-secret] }}"
    )
    assert credential_step["env"]["SWIFT_SECRET_NAME"] == "${{ steps.env_info.outputs.s3-swift-secret }}"
    assert "scripts/load-swift-config.py" in credential_step["run"]
    assert "SWIFT_CONFIG" not in step["env"]
    assert credential_step["if"] == "${{ success() && steps.env_info.outputs.swift-storage == 'true' }}"
    assert install_step["if"] == "${{ success() && steps.s3_credentials.outputs.available == 'true' }}"
    assert step["if"] == "${{ success() && steps.s3_credentials.outputs.available == 'true' }}"


def test_cleanup_entrypoint_passes_namespace_and_age_to_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location("cleanup_s3", ROOT / "scripts/cleanup-s3.py")
    assert spec is not None and spec.loader is not None
    cleanup_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleanup_script)

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

    def provider_factory(environ: Any, used_session: SessionStub) -> ProviderStub:
        observed["namespace"] = environ["S3_BUCKET_NAMESPACE"]
        observed["session"] = used_session
        return provider

    monkeypatch.setenv("S3_BUCKET_NAMESPACE", "test-storage-namespace")
    monkeypatch.setattr(sys, "argv", ["cleanup-s3.py", "--age-minutes", "17.5"])
    monkeypatch.setattr(cleanup_script.requests, "Session", lambda: session)
    monkeypatch.setattr(cleanup_script, "provider_from_environment", provider_factory)

    cleanup_script.main()

    assert observed == {"namespace": "test-storage-namespace", "session": session}
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
module.provider_from_environment = lambda environ, session: FailingProviderStub()
sys.argv = ["cleanup-s3.py"]
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
        ["python3", "scripts/load-swift-config.py", "S3_SWIFT_TEST"],
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
        ["python3", "scripts/load-swift-config.py", "S3_SWIFT_TEST", "--optional"],
        capture_output=True,
        check=False,
        env={**os.environ, "SWIFT_CONFIG": "", "GITHUB_OUTPUT": str(output)},
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0
    assert output.read_text() == "available=false\n"


def test_optional_swift_config_rejects_partial_values(tmp_path: Path) -> None:
    result = subprocess.run(
        ["python3", "scripts/load-swift-config.py", "S3_SWIFT_TEST", "--optional"],
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


def test_swift_config_loader_exports_optional_endpoint_and_region(tmp_path: Path) -> None:
    environment_file = tmp_path / "github-env"
    values = {name: "configured" for name in SWIFT_CONFIG_VARIABLES}
    values["S3_ENDPOINT"] = "https://s3.example"
    values["S3_REGION"] = "region-1"
    config = "\n".join(f"{name}={value}" for name, value in values.items())
    result = subprocess.run(
        ["python3", "scripts/load-swift-config.py", "S3_SWIFT_TEST", "--prefix", "TARGET_"],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "SWIFT_CONFIG": config,
            "GITHUB_ENV": str(environment_file),
        },
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0
    assert "TARGET_S3_ENDPOINT=https://s3.example" in environment_file.read_text().splitlines()
    assert "TARGET_S3_REGION=region-1" in environment_file.read_text().splitlines()


def test_swift_config_loader_preserves_equals_and_masks_values(tmp_path: Path) -> None:
    environment_file = tmp_path / "github-env"
    output_file = tmp_path / "github-output"
    values = {name: "configured" for name in SWIFT_CONFIG_VARIABLES}
    values["S3_OS_PASSWORD"] = "first=second%value"
    values["S3_ENDPOINT"] = "https://s3.example"
    config = "# config comment\n" + "\n".join(f"{name}={value}" for name, value in values.items())
    result = subprocess.run(
        ["python3", "scripts/load-swift-config.py", "S3_SWIFT_NEIGHBOR", "--prefix", "NEIGHBOR_", "--optional"],
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
    assert environment_file.read_text().splitlines() == [
        *(f"NEIGHBOR_{name}={value}" for name, value in values.items()),
        "NEIGHBOR_S3_BUCKET_NAMESPACE=S3_SWIFT_NEIGHBOR",
    ]
    assert output_file.read_text() == "available=true\n"


@pytest.mark.parametrize(
    ("current_state", "expected"),
    [
        pytest.param(State.NO_CONTROLLER, True, id="new-controller"),
        pytest.param(State.DEPLOYED, False, id="resumed-controller"),
    ],
)
def test_session_s3_cleanup_only_runs_when_this_run_owns_controller(current_state: State, expected: bool) -> None:
    assert can_cleanup_session_storage(current_state) is expected
