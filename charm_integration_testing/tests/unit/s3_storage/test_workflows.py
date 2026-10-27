# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from s3_storage.configuration import parse_s3_config, provider_from_config
from test_suite.fixtures.s3 import can_cleanup_session_storage
from test_suite.scheduler.states import State

ROOT = Path(__file__).resolve().parents[4]
S3_STORAGE_CONFIG_VARIABLES = (
    "S3_ENDPOINT",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_BUCKET_NAMESPACE",
    "S3_REGION",
    "S3_CLEANUP_ACCESS_KEY",
    "S3_CLEANUP_SECRET_KEY",
)
TARGET_SECRET_EXPRESSION = "${{ secrets[steps.target_env_info.outputs.s3-secret] }}"
NEIGHBOR_SECRET_EXPRESSION = "${{ secrets[steps.neighbor_env_info.outputs.s3-secret] }}"


def test_ci_s3_secret_lookup_uses_trusted_environment_metadata() -> None:
    # GIVEN environment metadata as the source of truth for the configured secret name
    workflow = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())
    job = workflow["jobs"]["execute-charm-test"]
    assert "S3_STORAGE_SECRET_NAMES" not in job["env"]
    assert all(not name.startswith("S3_STORAGE_CONFIG_") for name in job["env"])

    # WHEN each environment selects a secret, THEN the workflow dereferences that exact name
    for side in ("target", "neighbor"):
        step = next(step for step in job["steps"] if step.get("id") == f"{side}_s3_config")
        selected = f"steps.{side}_env_info.outputs.s3-secret"
        assert step["env"]["S3_STORAGE_CONFIG"] == f"${{{{ secrets[{selected}] }}}}"
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
        assert "S3_STORAGE_CONFIG" not in environment
        assert "S3_BUCKET_NAMESPACE" not in environment
        assert environment["S3_CONFIG_target_cloud"] == "${{ steps.target_s3_config.outputs.config_path }}"
    cmr = next(step for step in test_steps if step["id"] == "run_tests_cmr")
    assert cmr["env"]["S3_CONFIG_neighbor_cloud"] == "${{ steps.neighbor_s3_config.outputs.config_path }}"
    assert "steps.setup_target.outputs.cloud-name" in cmr["env"]["neighbor_cloud"]
    assert "inputs.same_controller" in cmr["env"]["neighbor_cloud"]


def test_ci_checks_each_environment_s3_secret_before_test_entrypoints() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())
    steps = workflow["jobs"]["execute-charm-test"]["steps"]
    test_steps = [step for step in steps if step.get("id") in ("run_tests", "run_tests_cmr")]
    target_check = next(step for step in steps if step.get("name") == "Check target S3 storage credentials")
    neighbor_check = next(step for step in steps if step.get("name") == "Check neighbor S3 storage credentials")

    assert len(test_steps) == 2
    assert "continue-on-error" not in target_check
    assert target_check["if"] == "${{ steps.target_env_info.outputs.s3-secret != '' }}"
    assert target_check["env"]["S3_STORAGE_SECRET_NAME"] == "${{ steps.target_env_info.outputs.s3-secret }}"
    assert target_check["env"]["S3_STORAGE_CONFIG"] == TARGET_SECRET_EXPRESSION
    assert "scripts/load-s3-config.py" in target_check["run"]
    assert target_check["id"] == "target_s3_config"
    assert '"${RUNNER_TEMP}/target-s3.conf"' in target_check["run"]
    assert neighbor_check["if"] == (
        "${{ inputs.neighbor_environment != '' && steps.neighbor_env_info.outputs.s3-secret != '' }}"
    )
    assert neighbor_check["env"]["S3_STORAGE_SECRET_NAME"] == "${{ steps.neighbor_env_info.outputs.s3-secret }}"
    assert neighbor_check["env"]["S3_STORAGE_CONFIG"] == NEIGHBOR_SECRET_EXPRESSION
    assert neighbor_check["id"] == "neighbor_s3_config"
    assert '"${RUNNER_TEMP}/neighbor-s3.conf"' in neighbor_check["run"]
    test_start = next(step for step in steps if step.get("name") == "Post 'test_start' event")
    assert max(steps.index(target_check), steps.index(neighbor_check)) < min(
        steps.index(test_start), *(steps.index(step) for step in test_steps)
    )


@pytest.mark.parametrize(
    ("secret_name", "prefix", "missing_variable"),
    [
        pytest.param("S3_STORAGE_TEST", "TARGET_", "S3_SECRET_KEY", id="target-missing"),
        pytest.param("S3_STORAGE_NEIGHBOR", "NEIGHBOR_", "S3_ACCESS_KEY", id="neighbor-missing"),
        pytest.param("S3_STORAGE_TEST", "TARGET_", "S3_BUCKET_NAMESPACE", id="target-missing-namespace"),
        pytest.param("S3_STORAGE_NEIGHBOR", "NEIGHBOR_", "S3_BUCKET_NAMESPACE", id="neighbor-missing-namespace"),
        pytest.param("S3_STORAGE_TEST", "TARGET_", None, id="target-complete"),
        pytest.param("S3_STORAGE_NEIGHBOR", "NEIGHBOR_", None, id="neighbor-complete"),
    ],
)
def test_ci_preflight_checks_environment_secrets(
    tmp_path: Path, secret_name: str, prefix: str, missing_variable: str | None
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/charm-testing.yaml").read_text())
    steps = workflow["jobs"]["execute-charm-test"]["steps"]
    check_name = (
        "Check target S3 storage credentials" if prefix == "TARGET_" else "Check neighbor S3 storage credentials"
    )
    preflight = next(step for step in steps if step.get("name") == check_name)
    values = {name: "configured" for name in S3_STORAGE_CONFIG_VARIABLES}
    values["S3_BUCKET_NAMESPACE"] = "shared-storage"
    if missing_variable is not None:
        values[missing_variable] = ""
    config = "\n".join(f"{name}={value}" for name, value in values.items())
    environment = {
        "S3_STORAGE_SECRET_NAME": secret_name,
        "S3_STORAGE_CONFIG": config,
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
        assert parse_s3_config(path.read_text(), S3_STORAGE_CONFIG_VARIABLES) == values
        assert path.stat().st_mode & 0o777 == 0o600
        assert Path(environment["GITHUB_OUTPUT"]).read_text() == f"config_path={path}\navailable=true\n"
        assert not Path(environment["GITHUB_ENV"]).exists()
    else:
        assert result.returncode == 1
        assert f"{secret_name} is missing: {missing_variable}" in result.stdout


def test_environment_info_exposes_optional_s3_secret() -> None:
    action = yaml.safe_load((ROOT / ".github/actions/get-environment-info/action.yaml").read_text())
    lookup = action["runs"]["steps"][0]["run"]
    environment_lookups = [line for line in lookup.splitlines() if "yq " in line and ".environments[" in line]

    assert "swift-storage" not in action["outputs"]
    assert action["outputs"]["s3-secret"]["value"] == "${{ steps.lookup.outputs.s3-secret }}"
    assert "s3-bucket-namespace" not in action["outputs"]
    assert any("s3_secret=$(yq -r" in line and "s3-secret" in line and '// ""' in line for line in lookup.splitlines())
    assert "swift_storage" not in lookup
    assert 'echo "s3-secret=${s3_secret}"' in lookup
    environments = yaml.safe_load((ROOT / "static/environments.yaml").read_text())["environments"]
    assert all("swift-storage" not in environment for environment in environments.values())
    assert environment_lookups
    assert all("yq -r" in line for line in environment_lookups if " -o=json " not in line)
    assert all("strenv(ENVIRONMENT_NAME)" in line for line in environment_lookups)
    assert all("${ENVIRONMENT_NAME}" not in line for line in environment_lookups)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        pytest.param("OS_PASSWORD=unrecognized", "Invalid S3_STORAGE_CONFIG entry on line 1", id="unknown-variable"),
        pytest.param(
            "S3_SECRET_KEY=first\nS3_SECRET_KEY=second",
            "Duplicate S3_STORAGE_CONFIG entry for S3_SECRET_KEY",
            id="duplicate-variable",
        ),
    ],
)
def test_s3_config_loader_rejects_invalid_entries(tmp_path: Path, config: str, message: str) -> None:
    environment = {
        **os.environ,
        "S3_STORAGE_CONFIG": config,
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
    }
    result = subprocess.run(
        [
            "python3",
            "scripts/load-s3-config.py",
            "S3_STORAGE_TEST",
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


def test_optional_s3_config_skips_when_missing(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    result = subprocess.run(
        [
            "python3",
            "scripts/load-s3-config.py",
            "S3_STORAGE_TEST",
            "--optional",
            "--output-file",
            str(tmp_path / "s3.conf"),
        ],
        capture_output=True,
        check=False,
        env={**os.environ, "S3_STORAGE_CONFIG": "", "GITHUB_OUTPUT": str(output)},
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0
    assert output.read_text() == "available=false\n"
    assert not (tmp_path / "s3.conf").exists()


def test_optional_s3_config_rejects_partial_values(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "python3",
            "scripts/load-s3-config.py",
            "S3_STORAGE_TEST",
            "--optional",
            "--output-file",
            str(tmp_path / "s3.conf"),
        ],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "S3_STORAGE_CONFIG": "S3_ENDPOINT=https://s3.example",
            "GITHUB_ENV": str(tmp_path / "github-env"),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        },
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 1
    assert "S3_STORAGE_TEST is missing: " in result.stdout
    assert "S3_SECRET_KEY" in result.stdout


@pytest.mark.parametrize("variable", S3_STORAGE_CONFIG_VARIABLES[:4])
@pytest.mark.parametrize("optional", [False, True], ids=["required", "optional"])
def test_s3_config_loader_rejects_whitespace_only_required_values(
    tmp_path: Path, variable: str, optional: bool
) -> None:
    # GIVEN a declared configuration with one required value containing only whitespace
    values = {name: "configured" for name in S3_STORAGE_CONFIG_VARIABLES}
    values[variable] = " \t "
    config_path = tmp_path / "s3.conf"
    output = tmp_path / "github-output"

    # WHEN the loader runs, THEN no protected configuration or success output is created
    result = subprocess.run(
        [
            sys.executable,
            "scripts/load-s3-config.py",
            "S3_STORAGE_TEST",
            "--output-file",
            str(config_path),
            *(["--optional"] if optional else []),
        ],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "S3_STORAGE_CONFIG": "\n".join(f"{name}={value}" for name, value in values.items()),
            "GITHUB_OUTPUT": str(output),
        },
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 1
    assert f"S3_STORAGE_TEST is missing: {variable}" in result.stdout
    assert not config_path.exists()
    assert not output.exists()


def test_s3_config_loader_writes_optional_region_and_separate_cleanup_identity(tmp_path: Path) -> None:
    environment_file = tmp_path / "github-env"
    values = {name: "configured" for name in S3_STORAGE_CONFIG_VARIABLES}
    values["S3_ENDPOINT"] = "https://s3.example"
    values["S3_REGION"] = "region-1"
    values["S3_CLEANUP_ACCESS_KEY"] = "cleanup-access"
    config = "\n".join(f"{name}={value}" for name, value in values.items())
    result = subprocess.run(
        [
            "python3",
            "scripts/load-s3-config.py",
            "S3_STORAGE_TEST",
            "--output-file",
            str(tmp_path / "s3.conf"),
        ],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "S3_STORAGE_CONFIG": config,
            "GITHUB_ENV": str(environment_file),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        },
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0
    config_values = parse_s3_config((tmp_path / "s3.conf").read_text(), S3_STORAGE_CONFIG_VARIABLES)
    assert config_values["S3_ENDPOINT"] == "https://s3.example"
    assert config_values["S3_CLEANUP_ACCESS_KEY"] == "cleanup-access"
    assert config_values["S3_REGION"] == "region-1"
    assert not environment_file.exists()


def test_s3_config_loader_omits_blank_optional_cleanup_credentials(tmp_path: Path) -> None:
    # GIVEN an otherwise valid config with both optional cleanup values blank
    values = {name: "configured" for name in S3_STORAGE_CONFIG_VARIABLES[:4]}
    values["S3_ENDPOINT"] = "https://s3.example"
    values.update(S3_CLEANUP_ACCESS_KEY="", S3_CLEANUP_SECRET_KEY="")
    config_path = tmp_path / "s3.conf"
    result = subprocess.run(
        ["python3", "scripts/load-s3-config.py", "S3_STORAGE_TEST", "--output-file", str(config_path)],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "S3_STORAGE_CONFIG": "\n".join(f"{name}={value}" for name, value in values.items()),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        },
        text=True,
        cwd=ROOT,
    )

    # WHEN the loader writes the protected config, THEN empty optional fields are absent
    assert result.returncode == 0
    loaded = parse_s3_config(config_path.read_text(), S3_STORAGE_CONFIG_VARIABLES)
    assert "S3_CLEANUP_ACCESS_KEY" not in loaded
    assert "S3_CLEANUP_SECRET_KEY" not in loaded
    assert provider_from_config(config_path).endpoint == "https://s3.example"


@pytest.mark.parametrize("variable", ["S3_CLEANUP_ACCESS_KEY", "S3_CLEANUP_SECRET_KEY"])
def test_s3_config_loader_rejects_unpaired_cleanup_credentials(tmp_path: Path, variable: str) -> None:
    # GIVEN a configuration with only half of the optional cleanup identity
    values = {name: "configured" for name in S3_STORAGE_CONFIG_VARIABLES if not name.startswith("S3_CLEANUP_")}
    values[variable] = "configured"
    config_path = tmp_path / "s3.conf"
    # WHEN the loader runs, THEN it fails before writing the protected file
    result = subprocess.run(
        ["python3", "scripts/load-s3-config.py", "S3_STORAGE_TEST", "--output-file", str(config_path)],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "S3_STORAGE_CONFIG": "\n".join(f"{name}={value}" for name, value in values.items()),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        },
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 1
    assert "must set both or neither of" in result.stdout
    assert not config_path.exists()


def test_s3_config_loader_preserves_equals_and_masks_values(tmp_path: Path) -> None:
    environment_file = tmp_path / "github-env"
    output_file = tmp_path / "github-output"
    values = {name: "configured" for name in S3_STORAGE_CONFIG_VARIABLES}
    values["S3_SECRET_KEY"] = " first=second%value "
    values["S3_ENDPOINT"] = "https://s3.example"
    config = "# config comment\n" + "\n".join(f"{name}={value}" for name, value in values.items())
    result = subprocess.run(
        [
            "python3",
            "scripts/load-s3-config.py",
            "S3_STORAGE_NEIGHBOR",
            "--output-file",
            str(tmp_path / "s3.conf"),
            "--optional",
        ],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "S3_STORAGE_CONFIG": config,
            "GITHUB_ENV": str(environment_file),
            "GITHUB_OUTPUT": str(output_file),
        },
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0
    assert "first=second%25value" in result.stdout
    assert parse_s3_config((tmp_path / "s3.conf").read_text(), S3_STORAGE_CONFIG_VARIABLES) == values
    assert not environment_file.exists()
    assert output_file.read_text() == f"config_path={tmp_path / 's3.conf'}\navailable=true\n"


def test_s3_config_loader_does_not_overwrite_existing_credentials(tmp_path: Path) -> None:
    # GIVEN an existing file that could belong to another cloud
    path = tmp_path / "existing.conf"
    path.write_text("existing configuration\n")
    values = {name: "configured" for name in S3_STORAGE_CONFIG_VARIABLES}
    # WHEN the loader is given the same destination, THEN it fails without replacing it
    result = subprocess.run(
        [
            "python3",
            "scripts/load-s3-config.py",
            "S3_STORAGE_TEST",
            "--output-file",
            str(path),
        ],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "S3_STORAGE_CONFIG": "\n".join(f"{name}={value}" for name, value in values.items()),
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
