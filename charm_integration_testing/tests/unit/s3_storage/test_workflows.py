# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from s3_storage.configuration import parse_s3_config
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
TARGET_SECRET_EXPRESSION = (
    "${{ contains(fromJSON(env.S3_SWIFT_SECRET_NAMES), steps.target_env_info.outputs.s3-swift-secret) "
    "&& secrets[steps.target_env_info.outputs.s3-swift-secret] || '' }}"
)
NEIGHBOR_SECRET_EXPRESSION = (
    "${{ contains(fromJSON(env.S3_SWIFT_SECRET_NAMES), steps.neighbor_env_info.outputs.s3-swift-secret) "
    "&& secrets[steps.neighbor_env_info.outputs.s3-swift-secret] || '' }}"
)


def test_ci_swift_secret_lookup_is_restricted_before_dereferencing() -> None:
    # GIVEN a fixed workflow allowlist independent of checked-out environment metadata
    workflow = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())
    job = workflow["jobs"]["execute-charm-test"]
    allowed = json.loads(job["env"]["S3_SWIFT_SECRET_NAMES"])
    environments = yaml.safe_load((ROOT / "static/environments.yaml").read_text())["environments"]
    assert set(allowed) == {environment["s3-swift-secret"] for environment in environments.values()}
    assert len(allowed) == len(set(allowed))
    assert all(name.startswith("S3_SWIFT_CONFIG_") for name in allowed)

    # WHEN metadata selects an unrelated secret, THEN it cannot reach the dynamic lookup
    assert "PROD_SQA_KUBECONFIG" not in allowed
    assert "TEST_OBSERVER_TOKEN" not in allowed
    assert "" not in allowed
    for side in ("target", "neighbor"):
        step = next(step for step in job["steps"] if step.get("id") == f"{side}_s3_config")
        selected = f"steps.{side}_env_info.outputs.s3-swift-secret"
        assert step["env"]["SWIFT_CONFIG"] == (
            "${{ contains(fromJSON(env.S3_SWIFT_SECRET_NAMES), " f"{selected}) && secrets[{selected}] || '' }}}}"
        )


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
    assert target_check["id"] == "target_s3_config"
    assert '"${RUNNER_TEMP}/target-s3.conf"' in target_check["run"]
    assert neighbor_check["if"] == (
        "${{ inputs.neighbor_environment != '' && steps.neighbor_env_info.outputs.swift-storage == 'true' }}"
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
        assert parse_s3_config(path.read_text(), (*SWIFT_CONFIG_VARIABLES, "S3_BUCKET_NAMESPACE")) == {
            **values,
            "S3_BUCKET_NAMESPACE": secret_name,
        }
        assert path.stat().st_mode & 0o777 == 0o600
        assert Path(environment["GITHUB_OUTPUT"]).read_text() == f"config_path={path}\navailable=true\n"
        assert not Path(environment["GITHUB_ENV"]).exists()
    else:
        assert result.returncode == 1
        assert f"{secret_name} is missing: {missing_variable}" in result.stdout


def test_environment_info_exposes_swift_storage_flag() -> None:
    action = yaml.safe_load((ROOT / ".github/actions/get-environment-info/action.yaml").read_text())
    lookup = action["runs"]["steps"][0]["run"]
    environment_lookups = [line for line in lookup.splitlines() if "yq " in line and ".environments[" in line]

    assert action["outputs"]["swift-storage"]["value"] == "${{ steps.lookup.outputs.swift-storage }}"
    assert action["outputs"]["s3-swift-secret"]["value"] == "${{ steps.lookup.outputs.s3-swift-secret }}"
    assert any(
        "swift_storage=$(yq -r" in line and "swift-storage" in line and "// false" in line
        for line in lookup.splitlines()
    )
    assert "must set s3-swift-secret" in lookup
    assert 'echo "swift-storage=${swift_storage}"' in lookup
    assert environment_lookups
    assert all("yq -r" in line for line in environment_lookups if " -o=json " not in line)
    assert all("strenv(ENVIRONMENT_NAME)" in line for line in environment_lookups)
    assert all("${ENVIRONMENT_NAME}" not in line for line in environment_lookups)


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
        ["python3", "scripts/load-swift-config.py", "S3_SWIFT_TEST", "--output-file", str(tmp_path / "s3.conf")],
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


def test_swift_config_loader_writes_optional_endpoint_and_region(tmp_path: Path) -> None:
    environment_file = tmp_path / "github-env"
    values = {name: "configured" for name in SWIFT_CONFIG_VARIABLES}
    values["S3_ENDPOINT"] = "https://s3.example"
    values["S3_REGION"] = "region-1"
    config = "\n".join(f"{name}={value}" for name, value in values.items())
    result = subprocess.run(
        ["python3", "scripts/load-swift-config.py", "S3_SWIFT_TEST", "--output-file", str(tmp_path / "s3.conf")],
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
    config_values = parse_s3_config(
        (tmp_path / "s3.conf").read_text(), (*SWIFT_CONFIG_VARIABLES, "S3_BUCKET_NAMESPACE")
    )
    assert config_values["S3_ENDPOINT"] == "https://s3.example"
    assert config_values["S3_REGION"] == "region-1"
    assert not environment_file.exists()


def test_swift_config_loader_preserves_equals_and_masks_values(tmp_path: Path) -> None:
    environment_file = tmp_path / "github-env"
    output_file = tmp_path / "github-output"
    values = {name: "configured" for name in SWIFT_CONFIG_VARIABLES}
    values["S3_OS_PASSWORD"] = "first=second%value"
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
    assert parse_s3_config((tmp_path / "s3.conf").read_text(), (*SWIFT_CONFIG_VARIABLES, "S3_BUCKET_NAMESPACE")) == {
        **values,
        "S3_BUCKET_NAMESPACE": "S3_SWIFT_NEIGHBOR",
    }
    assert not environment_file.exists()
    assert output_file.read_text() == f"config_path={tmp_path / 's3.conf'}\navailable=true\n"


def test_swift_config_loader_does_not_overwrite_existing_credentials(tmp_path: Path) -> None:
    # GIVEN an existing file that could belong to another cloud
    path = tmp_path / "existing.conf"
    path.write_text("existing configuration\n")
    values = {name: "configured" for name in SWIFT_CONFIG_VARIABLES}
    # WHEN the loader is given the same destination, THEN it fails without replacing it
    result = subprocess.run(
        ["python3", "scripts/load-swift-config.py", "S3_SWIFT_TEST", "--output-file", str(path)],
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
