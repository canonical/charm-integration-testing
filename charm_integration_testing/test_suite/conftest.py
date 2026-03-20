# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


import json
import logging
import os
import warnings
from pathlib import Path
from subprocess import CalledProcessError, run  # nosec
from typing import Any, Callable, Iterator

import pytest
import requests
from extensions import (
    ConfigureLivepatchServerExtension,
    PostgresqlDatabaseReplicationExtension,
    PostgresqlK8sDatabaseReplicationExtension,
    S3IntegratorMinIOBackendExtension,
    TemporalExtension,
    UnsealVaultJujuExtension,
    UnsealVaultK8sJujuExtension,
    ValidatorInjectorExtension,
)
from juju import JujuBackend, JujuClient, JujuValidationError, JujuWaitTimeoutError
from juju_cmd.backend import JujuCmdBackend
from juju_jubilant import JubilantBackend
from kubernetes_client import KubernetesBackend, KubernetesClient
from pydantic import TypeAdapter, ValidationError
from pytest import StashKey
from utils import normalize_string, normalize_string_multiline

from bundle_builder import UnfulfilledEndpointsError
from test_suite.scheduler.markers import read_state_marker
from test_suite.scheduler.states import State

pytest_plugins = [
    "test_suite.scheduler.plugin",
]

KNOWN_FAILURE_EXCEPTIONS = (
    JujuWaitTimeoutError,
    JujuValidationError,
    AssertionError,
)


class TestObserverClient:
    """Thin client for querying historical charm test data from Test Observer."""

    def __init__(self, api_url: str | None, token: str | None) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.token = token

    @property
    def enabled(self) -> bool:
        return self.api_url is not None

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _ensure_api_url(self) -> str:
        if not self.api_url:
            raise RuntimeError("TEST_OBSERVER_API is not configured")
        return self.api_url

    def _json_object(self, response: requests.Response, endpoint: str) -> dict[str, Any] | list[dict[str, Any]]:
        payload = response.json()
        if not (isinstance(payload, dict) or isinstance(payload, list)):
            raise RuntimeError(f"Unexpected JSON payload from {endpoint}: expected object or list")
        return payload

    def _extract_first_list(self, payload: dict[str, Any] | list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _extract_id(self, item: dict[str, Any] | list[dict[str, Any]], *keys: str) -> int | None:
        if isinstance(item, list):
            for sub_item in item:
                result = self._extract_id(sub_item, *keys)
                if result is not None:
                    return result
            return None
        for key in keys:
            value = item.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None

    def _query_artefacts_history(self, stage: str, name: str, track: str) -> dict[str, Any] | list[dict[str, Any]]:
        api_url = self._ensure_api_url()
        params: dict[str, str | int] = {
            "family": "charm",
            "limit": 10,
            "offset": 0,
            "stage": stage,
            "name": name,
            "track": track,
        }
        response = requests.get(
            f"{api_url}/v1/artefacts/history",
            params=params,
            headers=self._build_headers(),
            timeout=30,
        )
        response.raise_for_status()
        return self._json_object(response, "/v1/artefacts/history")

    def _query_artefact_builds(self, artefact_id: int) -> dict[str, Any] | list[dict[str, Any]]:
        api_url = self._ensure_api_url()
        response = requests.get(
            f"{api_url}/v1/artefacts/{artefact_id}/builds",
            params={"limit": 100, "offset": 0},
            headers=self._build_headers(),
            timeout=30,
        )
        response.raise_for_status()
        return self._json_object(response, "/v1/artefacts/{artefact_id}/builds")

    def _query_test_results_for_execution(self, execution_id: int) -> dict[str, Any] | list[dict[str, Any]]:
        api_url = self._ensure_api_url()

        direct_response = requests.get(
            f"{api_url}/v1/test-executions/{execution_id}/test-results",
            headers=self._build_headers(),
            timeout=30,
        )
        if direct_response.status_code == 200:
            return {"test_results": direct_response.json()}

        errors: list[requests.HTTPError] = []
        for query_key in ("test_execution", "test_execution_id", "test_execution_ids"):
            response = requests.get(
                f"{api_url}/v1/test-results",
                params={query_key: execution_id, "limit": 200, "offset": 0},
                headers=self._build_headers(),
                timeout=30,
            )
            try:
                response.raise_for_status()
                return self._json_object(response, "/v1/test-results")
            except requests.HTTPError as exc:
                errors.append(exc)
                if response.status_code not in (400, 422):
                    raise

        raise RuntimeError(
            "Unable to query /v1/test-results for test execution "
            f"{execution_id}; all supported query parameters failed: {errors}"
        )

    def _has_test_deploy_passed(self, test_results_payload: dict[str, Any] | list[dict[str, Any]]) -> bool:
        test_results = self._extract_first_list(test_results_payload, "test_results", "results", "items")
        for result in test_results:
            if result.get("name") == "test_deploy" and str(result.get("status", "")).lower() == "passed":
                return True
        return False

    def choose_historical_revision_with_passing_deploy(
        self,
        charm_name: str,
        stage: str,
        current_revision: int,
        track: str,
    ) -> int | None:
        history_payload = self._query_artefacts_history(stage=stage, name=charm_name, track=track)
        artefacts = self._extract_first_list(history_payload, "artefacts", "items", "results", "data")

        for artefact in artefacts:
            artefact_id = self._extract_id(artefact, "id", "artefact_id", "artifact_id")
            if artefact_id is None:
                continue

            builds_payload = self._query_artefact_builds(artefact_id=artefact_id)
            builds = self._extract_first_list(builds_payload, "builds", "items", "results", "data")

            for build in builds:
                revision = self._extract_id(build, "revision")
                if revision is None or revision == current_revision:
                    continue

                executions = build.get("test_executions")
                if not isinstance(executions, list):
                    continue

                for execution in executions:
                    if not isinstance(execution, dict):
                        continue
                    execution_id = self._extract_id(execution, "id", "test_execution_id")
                    if execution_id is None:
                        continue

                    test_results_payload = self._query_test_results_for_execution(execution_id=execution_id)
                    if self._has_test_deploy_passed(test_results_payload):
                        return revision

        return None


@pytest.fixture
def test_observer_client() -> TestObserverClient:
    api_url = os.environ.get("TEST_OBSERVER_API") or os.environ.get("test_observer_api")
    token = os.environ.get("TEST_OBSERVER_TOKEN") or os.environ.get("test_observer_token")
    return TestObserverClient(api_url=api_url, token=token)


@pytest.fixture
def logger() -> logging.Logger:
    jubilant_logger = logging.getLogger("jubilant")
    jubilant_logger.setLevel(logging.WARNING)

    jubilant_logger_wait = logging.getLogger("jubilant.wait")
    jubilant_logger_wait.setLevel(logging.WARNING)

    return logging.getLogger()


@pytest.fixture
def juju_backend() -> JujuBackend:
    return JubilantBackend()


@pytest.fixture
def juju_client(
    juju_backend: JujuBackend,
    logger: logging.Logger,
    minio_client_file: Path | None,
    ubuntu_pro_token: str | None,
    uv_file: Path | None,
    validators_path: Path | None,
) -> JujuClient:
    return JujuClient(
        juju_backend,
        logger,
        extensions=[
            ConfigureLivepatchServerExtension(juju_backend, logger, ubuntu_pro_token),
            PostgresqlDatabaseReplicationExtension(juju_backend, logger),
            PostgresqlK8sDatabaseReplicationExtension(juju_backend, logger),
            S3IntegratorMinIOBackendExtension(juju_backend, logger, minio_client_file),
            TemporalExtension(juju_backend, logger),
            UnsealVaultJujuExtension(juju_backend, logger),
            UnsealVaultK8sJujuExtension(juju_backend, logger),
            ValidatorInjectorExtension(validators_path, juju_backend, logger, uv_file),
        ],
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--model", type=str, required=True, help="Juju model to test in.")
    parser.addoption(
        "--bundle",
        type=str,
        default=None,
        help="Bundle file path to deploy (used by deploy and idempotent-redeploy phases).",
    )
    parser.addoption(
        "--mermaid-output",
        type=str,
        default=None,
        help="File path to save the generated mermaid output.",
    )
    parser.addoption(
        "--target-application",
        type=str,
        default=None,
        help="Application under test (used by integration tests).",
    )
    parser.addoption(
        "--target-endpoint",
        type=str,
        default=None,
        help="Endpoint on the target application used for the integration under test.",
    )
    parser.addoption(
        "--neighbor-application",
        type=str,
        default=None,
        help="Neighbor application that integrates with the target (used by integration tests).",
    )
    parser.addoption(
        "--neighbor-endpoint",
        type=str,
        default=None,
        help="Endpoint on the neighbor application used for the integration under test.",
    )
    parser.addoption(
        "--target-charm",
        type=str,
        default=None,
        help="Charmhub name of the charm under test (used by bundle building).",
    )
    parser.addoption(
        "--neighbor-charm",
        type=str,
        default=None,
        help="Charmhub name of the neighbor charm (used by bundle building).",
    )
    parser.addoption(
        "--target-channel",
        type=str,
        default="default",
        help="Channel of the charm under test, e.g. '2/stable'. Use 'default' to defer to charm-default-versions.",
    )
    parser.addoption(
        "--target-revision",
        type=str,
        default="default",
        help="Revision of the charm under test (integer). Use 'default' to defer to charm-default-versions.",
    )
    parser.addoption(
        "--target-series",
        type=str,
        default="default",
        help="Ubuntu base series for the charm under test, e.g. '22.04'. Use 'default' to let the builder decide.",
    )
    parser.addoption(
        "--platform",
        type=str,
        default="kubernetes",
        help="Platform to deploy on (default: 'kubernetes').",
    )
    parser.addoption(
        "--charm-metadata-overrides",
        type=str,
        default="./static/charm-metadata-overrides/",
        help="Directory to the charm-metadata-overrides.",
    )
    parser.addoption(
        "--charm-platform-overrides",
        type=str,
        default="./static/charm-platform-overrides/",
        help="Directory to the charm-platform-overrides.",
    )
    parser.addoption(
        "--charm-listing-overrides",
        type=str,
        default="./static/charm-listing-overrides.yaml",
        help="Path to the charm-listing-overrides yaml.",
    )
    parser.addoption(
        "--charm-test-configs",
        type=str,
        default="./static/charm-test-configs/",
        help="Directory to the charm-test-configs.",
    )
    parser.addoption(
        "--charm-priorities-config",
        type=str,
        default="./static/charm-priorities.yaml",
        help="Path to the charm-priorities yaml.",
    )
    parser.addoption(
        "--charm-default-versions",
        type=str,
        default="./static/charm-default-versions.yaml",
        help="Path to the charm-default-versions yaml.",
    )
    parser.addoption(
        "--juju-cloud",
        type=str,
        default=None,
        help="Name of the Juju Cloud to create the controller on.",
    )
    parser.addoption(
        "--juju-controller",
        type=str,
        default="charmqa",
        help="Name of the controller to create the model on.",
    )
    parser.addoption(
        "--juju-model-config",
        type=str,
        default=None,
        help="Path to a json file containing the model configurations to be passed down to Juju on model creation.",
    )
    parser.addoption(
        "--juju-controller-bootstrap-constraints",
        type=str,
        default=None,
        help="Path to a json file containing the controller constraints configurations to be passed down to Juju on controller bootstrap.",
    )


@pytest.fixture
def model(request: pytest.FixtureRequest) -> str:
    option = request.config.getoption("--model")
    assert isinstance(option, str)
    return option


@pytest.fixture
def juju_model_config(request: pytest.FixtureRequest) -> dict[str, str]:
    """Juju model config file path passed via ``--juju-model-config``."""
    value = request.config.getoption("--juju-model-config")

    if not value:
        return dict()

    assert isinstance(value, str)
    value = Path(value).resolve()
    if not value.exists() or not value.is_file():
        pytest.fail(
            "Juju model config file passed via the --juju-model-config parameter does not exist or is not a file."
        )

    # Define the expected shape
    ConfigSchema = TypeAdapter(dict[str, str])

    try:
        content = json.loads(value.read_text())
        return ConfigSchema.validate_python(content)
    except ValidationError as e:
        pytest.fail(f"Invalid Juju model config passed via the --juju-model-config parameter: {e}")
    except json.JSONDecodeError as e:
        pytest.fail(
            f"Juju model config file passed via the --juju-model-config parameter does not contain valid JSON: {e}"
        )


@pytest.fixture
def juju_controller_bootstrap_constraints(request: pytest.FixtureRequest) -> dict[str, str]:
    """Juju controller bootstrap constraints config file path passed via ``--juju-controller-bootstrap-constraints``."""
    value = request.config.getoption("--juju-controller-bootstrap-constraints")

    if not value:
        return dict()

    assert isinstance(value, str)
    value = Path(value).resolve()
    if not value.exists() or not value.is_file():
        pytest.fail(
            "Juju controller bootstrap constraints config file passed via the --juju-controller-bootstrap-constraints parameter does not exist or is not a file."
        )

    # Define the expected shape
    ConfigSchema = TypeAdapter(dict[str, str])

    try:
        content = json.loads(value.read_text())
        return ConfigSchema.validate_python(content)
    except ValidationError as e:
        pytest.fail(
            f"Invalid Juju controller bootstrap constraints config passed via the --juju-controller-bootstrap-constraints parameter: {e}"
        )
    except json.JSONDecodeError as e:
        pytest.fail(
            f"Juju controller bootstrap constraints config file passed via the --juju-controller-bootstrap-constraints parameter does not contain valid JSON: {e}"
        )


@pytest.fixture
def bundle(request: pytest.FixtureRequest) -> Path:
    """Bundle file path passed via ``--bundle``."""
    value = request.config.getoption("--bundle")

    if not value:
        return Path(request.config.rootpath) / "generated-bundle.yaml"

    assert isinstance(value, str)
    value = Path(value).resolve()
    # Ensures parents path exists for the output when calling .write_text
    value.parent.mkdir(parents=True, exist_ok=True)
    return value


@pytest.fixture
def target_application(request: pytest.FixtureRequest) -> str:
    """Name of the charm application under test, passed via ``--target-application``."""
    value = request.config.getoption("--target-application")
    if not value:
        pytest.fail("--target-application is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture
def target_endpoint(request: pytest.FixtureRequest) -> str:
    """Juju endpoint on the target application, passed via ``--target-endpoint``."""
    value = request.config.getoption("--target-endpoint")
    if not value:
        pytest.fail("--target-endpoint is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture
def neighbor_application(request: pytest.FixtureRequest) -> str:
    """Name of the neighbor application, passed via ``--neighbor-application``."""
    value = request.config.getoption("--neighbor-application")
    if not value:
        pytest.fail("--neighbor-application is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture
def neighbor_endpoint(request: pytest.FixtureRequest) -> str:
    """Juju endpoint on the neighbor application, passed via ``--neighbor-endpoint``."""
    value = request.config.getoption("--neighbor-endpoint")
    if not value:
        pytest.fail("--neighbor-endpoint is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture
def validators_path() -> Path | None:
    file_path_env = os.environ.get("VALIDATORS_PATH")
    if not file_path_env:
        return None
    file_path = Path(file_path_env.strip())
    assert file_path.is_dir(), f"Validators path is invalid: {file_path}"
    return file_path


@pytest.fixture
def target_charm(request: pytest.FixtureRequest) -> str:
    """Charmhub name of the charm under test, passed via ``--target-charm``."""
    value = request.config.getoption("--target-charm")
    if not value:
        pytest.fail("--target-charm is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture
def neighbor_charm(request: pytest.FixtureRequest) -> str:
    """Charmhub name of the neighbor charm, passed via ``--neighbor-charm``."""
    value = request.config.getoption("--neighbor-charm")
    if not value:
        pytest.fail("--neighbor-charm is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture
def target_channel(request: pytest.FixtureRequest) -> str | None:
    """Channel of the charm under test.

    Returns ``None`` when the value is ``"default"``, which tells
    ``CharmhubClient.charm_from_store`` to defer to ``charm-default-versions.yaml``.
    """
    value = request.config.getoption("--target-channel")
    return None if value == "default" else value


@pytest.fixture
def target_revision(request: pytest.FixtureRequest) -> int | None:
    """Revision of the charm under test.

    Returns ``None`` when the value is ``"default"``, which tells
    ``CharmhubClient.charm_from_store`` to defer to ``charm-default-versions.yaml``.
    """
    value = request.config.getoption("--target-revision")
    return None if value == "default" else int(value)


@pytest.fixture
def target_series(request: pytest.FixtureRequest) -> str | None:
    """Ubuntu base series for the charm under test.

    Returns ``None`` when the value is ``"default"``, which tells
    ``CharmhubClient.charm_from_store`` to pick a series based on the charm metadata.
    """
    value = request.config.getoption("--target-series")
    return None if value == "default" else value


@pytest.fixture
def platform(request: pytest.FixtureRequest) -> str:
    value = request.config.getoption("--platform")
    if not value:
        pytest.fail("--platform is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture
def juju_cloud(request: pytest.FixtureRequest) -> str:
    value = request.config.getoption("--juju-cloud")
    if not value:
        pytest.fail("--juju-cloud is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture
def juju_controller(request: pytest.FixtureRequest) -> str:
    value = request.config.getoption("--juju-controller")
    if not value:
        pytest.fail("--juju-controller is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture
def charm_metadata_overrides(request: pytest.FixtureRequest) -> Path:
    value = request.config.getoption("--charm-metadata-overrides")
    if not value:
        pytest.fail("--charm-metadata-overrides is required by this test but was not provided.")
    assert isinstance(value, str)
    ppath = Path(value).resolve()
    if not ppath.exists():
        pytest.fail("Provided path for --charm-metadata-overrides does not exist.")
    return ppath


@pytest.fixture
def charm_platform_overrides(request: pytest.FixtureRequest) -> Path:
    value = request.config.getoption("--charm-platform-overrides")
    if not value:
        pytest.fail("--charm-platform-overrides is required by this test but was not provided.")
    assert isinstance(value, str)
    ppath = Path(value).resolve()
    if not ppath.exists():
        pytest.fail("Provided path for --charm-platform-overrides does not exist.")
    return ppath


@pytest.fixture
def charm_listing_overrides(request: pytest.FixtureRequest) -> Path:
    value = request.config.getoption("--charm-listing-overrides")
    if not value:
        pytest.fail("--charm-listing-overrides is required by this test but was not provided.")
    assert isinstance(value, str)
    ppath = Path(value).resolve()
    if not ppath.exists():
        pytest.fail("Provided path for --charm-listing-overrides does not exist.")
    return ppath


@pytest.fixture
def charm_test_configs(request: pytest.FixtureRequest) -> Path:
    value = request.config.getoption("--charm-test-configs")
    if not value:
        pytest.fail("--charm-test-configs is required by this test but was not provided.")
    assert isinstance(value, str)
    ppath = Path(value).resolve()
    if not ppath.exists():
        pytest.fail("Provided path for --charm-test-configs does not exist.")
    return ppath


@pytest.fixture
def charm_priorities_config(request: pytest.FixtureRequest) -> Path:
    value = request.config.getoption("--charm-priorities-config")
    if not value:
        pytest.fail("--charm-priorities-config is required by this test but was not provided.")
    assert isinstance(value, str)
    ppath = Path(value).resolve()
    if not ppath.exists():
        pytest.fail("Provided path for --charm-priorities-config does not exist.")
    return ppath


@pytest.fixture
def charm_default_versions(request: pytest.FixtureRequest) -> Path:
    value = request.config.getoption("--charm-default-versions")
    if not value:
        pytest.fail("--charm-default-versions is required by this test but was not provided.")
    assert isinstance(value, str)
    ppath = Path(value).resolve()
    if not ppath.exists():
        pytest.fail("Provided path for --charm-default-versions does not exist.")
    return ppath


@pytest.fixture
def bundle_mermaid_output(request: pytest.FixtureRequest) -> Path:
    """Path where the generated bundle Mermaid diagram is written by ``test_build_bundle``."""
    value = request.config.getoption("--mermaid-output")
    if not value:
        pytest.fail("--mermaid-output is required by this test but was not provided.")
    assert isinstance(value, str)
    ppath = Path(value).resolve()
    # Ensures parents path exists for the output when calling .write_text
    ppath.parent.mkdir(parents=True, exist_ok=True)
    return ppath


@pytest.fixture
def minio_client_file() -> Path | None:
    file_path = os.environ.get("MINIO_CLIENT_FILE")
    if file_path:
        file_path = file_path.strip()
    return Path(file_path) if file_path else None


@pytest.fixture
def uv_file() -> Path | None:
    file_path = os.environ.get("UV_FILE")
    if file_path:
        file_path = file_path.strip()
    return Path(file_path) if file_path else None


@pytest.fixture
def ubuntu_pro_token() -> str | None:
    token = os.environ.get("UBUNTU_PRO_TOKEN")
    if token:
        token = token.strip()
    return token if token else None


failure_message: StashKey[str] = StashKey()
error_message: StashKey[str] = StashKey()
skipped_message: StashKey[str] = StashKey()
failure_exception: StashKey[BaseException] = StashKey()


# Get failure message for logging
@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    result = yield
    assert result is not None
    report = result.get_result()

    unexpected_error = False

    if call.excinfo is not None:
        exception_type = call.excinfo.type
        # Don't interfere with pytest's built-in exceptions (skip, xfail, etc.)
        if exception_type.__name__ in ("Skipped", "XFailed", "Exit"):
            pass
        elif exception_type not in KNOWN_FAILURE_EXCEPTIONS:
            # Unexpected errors: set flag to modify message
            unexpected_error = True

    # Save failure message
    if report.failed:
        # Adapted from https://docs.pytest.org/en/stable/_modules/_pytest/junitxml.html
        reprcrash = getattr(report.longrepr, "reprcrash", None)
        if reprcrash is not None:
            item.stash[failure_message] = reprcrash.message
        else:
            item.stash[failure_message] = str(report.longrepr)
        if unexpected_error:
            item.stash[error_message] = item.stash[failure_message]
    # Save skip message
    if report.skipped:
        # Adapted from https://docs.pytest.org/en/stable/_modules/_pytest/junitxml.html
        if hasattr(report, "wasxfail"):
            item.stash[skipped_message] = report.wasxfail.removeprefix("reason: ")
        else:
            if isinstance(report.longrepr, tuple) and len(report.longrepr) >= 3:
                _, _, skipreason = report.longrepr
            else:
                skipreason = str(report.longrepr)
            item.stash[skipped_message] = skipreason.removeprefix("Skipped: ")

    # Save failure exception
    if call.excinfo:
        item.stash[failure_exception] = call.excinfo.value


@pytest.fixture(autouse=True)
def print_setup_and_teardown_info(
    request: pytest.FixtureRequest,
    logger: logging.Logger,
    juju_client: JujuClient,
    model: str,
    record_execution_metadata: None,
) -> Iterator[None]:
    state_marker = read_state_marker(request.node)
    if not (
        state_marker
        and any(state in (State.NO_MODEL, State.NO_CONTROLLER, State.NO_BUNDLE) for state in state_marker.requires)
    ):
        # Enforce fixture execution order
        _ = record_execution_metadata

        # Print starting state
        juju_client.print_status(model=model)

    # Log starting
    logger.info(f"Starting {request.node.name}")

    yield

    # Log error
    if error_message in request.node.stash:
        logger.error(f"Error in {request.node.name}: {request.node.stash[error_message]}")
    elif failure_message in request.node.stash:
        logger.error(f"Failure in {request.node.name}: {request.node.stash[failure_message]}")
    elif skipped_message in request.node.stash:
        logger.info(f"Skipped {request.node.name}: {request.node.stash[skipped_message]}")
    else:
        logger.info(f"Successfully ran {request.node.name}")

    if not (state_marker and state_marker.provides in (State.NO_MODEL, State.NO_CONTROLLER, State.NO_BUNDLE)):
        # Log ending state
        juju_client.print_status(model=model)


@pytest.fixture
def execution_metadata(record_property: Callable[[str, object], None]) -> Iterator[Callable[[str, str], None]]:
    # Create a function for adding and deduplicating metadata
    metadata: dict[str, set[str]] = {}

    def add(category: str, value: str) -> None:
        if not category.strip() or not value.strip():
            return
        if category not in metadata:
            metadata[category] = set()
        metadata[category].add(value)

    # Provide the function
    yield add

    # After the test, record all the metadata
    for category, values in metadata.items():
        # JUnit properties are key value, where one key can only be mapped to one value
        # Execution metadata is category value, where one category can be mapped to multiple values
        # So just store the values as a list in the single key
        # and use JSON to ensure characters are escaped properly
        record_property(category, json.dumps([value for value in sorted(values)]))


@pytest.fixture(autouse=True)
def record_execution_metadata(
    record_warning_execution_metadata: None,
    record_failure_execution_metadata: None,
    record_juju_execution_metadata: None,
    record_charms_and_revisions_execution_metadata: None,
    record_pipeline_version_execution_metadata: None,
) -> None:
    # Save various execution metadata
    _ = record_warning_execution_metadata
    _ = record_failure_execution_metadata
    _ = record_juju_execution_metadata
    _ = record_charms_and_revisions_execution_metadata
    _ = record_pipeline_version_execution_metadata


@pytest.fixture
def record_warning_execution_metadata(execution_metadata: Callable[[str, str | int], None]) -> Iterator[None]:
    # Capture all warnings
    # Pytest normally captures warnings, but does not expose them until after the test report is made
    captured_warnings = []
    with warnings.catch_warnings(record=True) as warnings_list:
        # Let the test run
        yield

        # Save all warnings
        for warning in warnings_list:
            execution_metadata("warning:message", normalize_string(f"{warning.category.__name__}: {warning.message}"))
            captured_warnings.append(warning)

    # Re-emit all warnings so they show up in the test summary
    for warning in captured_warnings:
        warnings.warn_explicit(
            message=warning.message,
            category=warning.category,
            filename=warning.filename,
            lineno=warning.lineno,
            source=warning.source,
        )


def record_charms_and_revisions_execution_metadata_instantaneous(
    juju_client: JujuClient, model: str, execution_metadata: Callable[[str, str | int], None]
) -> None:
    # Get all charm revisions
    applications = juju_client.list_applications(model=model)
    for application_info in applications.values():
        # Save the charm
        execution_metadata("charm", application_info.charm)
        # Save the revision
        execution_metadata(f"charm:{application_info.charm}:revision", str(application_info.revision))

    # Get all integrations and record them
    for integration in juju_client.list_integrations(model=model):
        # Record integration in format: provider:endpoint/interface/requirer:endpoint
        integration_str = (
            f"{applications[integration.provider.application].charm}:{integration.provider.endpoint}/"
            f"{integration.interface}/"
            f"{applications[integration.requirer.application].charm}:{integration.requirer.endpoint}"
        )
        execution_metadata("integration", integration_str)


@pytest.fixture
def record_charms_and_revisions_execution_metadata(
    request: pytest.FixtureRequest,
    juju_client: JujuClient,
    model: str,
    execution_metadata: Callable[[str, str | int], None],
) -> Iterator[None]:
    state_marker = read_state_marker(request.node)
    if not (
        state_marker
        and any(state in (State.NO_MODEL, State.NO_CONTROLLER, State.NO_BUNDLE) for state in state_marker.requires)
    ):
        # Save all charms and revisions at start of test
        record_charms_and_revisions_execution_metadata_instantaneous(juju_client, model, execution_metadata)

    # Let the test run
    yield
    if not (state_marker and state_marker.provides in (State.NO_MODEL, State.NO_CONTROLLER, State.NO_BUNDLE)):
        # Save all charms and revisions at end of test
        record_charms_and_revisions_execution_metadata_instantaneous(juju_client, model, execution_metadata)


@pytest.fixture
def record_failure_execution_metadata(
    request: pytest.FixtureRequest, execution_metadata: Callable[[str, str | int], None]
) -> Iterator[None]:
    # Let the test run
    yield

    # Save the failure message
    if failure_message in request.node.stash:
        execution_metadata("failure:message", normalize_string(request.node.stash[failure_message]))

    # Save the skip message
    if skipped_message in request.node.stash:
        execution_metadata("skipped:message", normalize_string(request.node.stash[skipped_message]))

    # Save extra metadata from exception
    if failure_exception in request.node.stash:
        exc = request.node.stash[failure_exception]

        if isinstance(exc, JujuWaitTimeoutError):
            for application in exc.wait_state.noncompliant_applications.values():
                if application is None:
                    continue
                execution_metadata(
                    f"failure:charm:{application.charm}:status",
                    f"application:{application.status}:{normalize_string(application.message)}",
                )
            for unit in exc.wait_state.noncompliant_units.values():
                if unit is None:
                    continue
                execution_metadata(
                    f"failure:charm:{unit.charm}:status",
                    f"unit:{unit.status}:{normalize_string(unit.message)}",
                )
            for unit_agent in exc.wait_state.noncompliant_unit_agents.values():
                if unit_agent is None:
                    continue
                execution_metadata(
                    f"failure:charm:{unit_agent.charm}:status",
                    f"unit_agent:{unit_agent.status}:{normalize_string(unit_agent.message)}",
                )
        elif isinstance(exc, CalledProcessError):
            cmd = " ".join(exc.cmd) if isinstance(exc.cmd, (list, tuple)) else exc.cmd
            execution_metadata("failure:cli:cmd", normalize_string(cmd))
            execution_metadata("failure:cli:return_code", str(exc.returncode))
            if exc.stdout:
                for line in normalize_string_multiline(exc.stdout):
                    execution_metadata("failure:cli:stdout", line)
            if exc.stderr:
                for line in normalize_string_multiline(exc.stderr):
                    execution_metadata("failure:cli:stderr", line)
        elif isinstance(exc, JujuValidationError):
            for results in exc.failed_validations.values():
                for result in results:
                    execution_metadata(f"failure:validator:interface:{result.interface}", result.status)
                    for check in result.checks:
                        if not check.passed:
                            execution_metadata(
                                f"failure:validator:interface:{result.interface}:check",
                                normalize_string(f"{check.name}: {check.message}"),
                            )
                    if result.error:
                        execution_metadata(
                            f"failure:validator:interface:{result.interface}:error",
                            normalize_string(result.error),
                        )
        elif isinstance(exc, UnfulfilledEndpointsError):
            for endpoint in exc.unfulfilled_endpoints:
                charm = exc.best_bundle.application_lookup[endpoint.application].charm.name
                interface = exc.best_bundle.application_endpoints[endpoint].interface
                execution_metadata("failure:build_bundle:unfulfilled_endpoint", f"{charm}:{endpoint.endpoint}")
                execution_metadata("failure:build_bundle:unfulfilled_interface", interface)

        if error_message in request.node.stash:
            # toggle expected failure flag
            execution_metadata("failure:expected", "false")
        else:
            execution_metadata("failure:expected", "true")


@pytest.fixture
def record_juju_execution_metadata(
    request: pytest.FixtureRequest,
    juju_client: JujuClient,
    model: str,
    execution_metadata: Callable[[str, str | int], None],
) -> Iterator[None]:
    state_marker = read_state_marker(request.node)
    if state_marker and any(state in (State.NO_MODEL, State.NO_CONTROLLER) for state in state_marker.requires):
        yield
        return

    # Let the test run
    yield

    # Save Juju version
    juju_version = juju_client.version(model)
    execution_metadata("juju:version", juju_version)


@pytest.fixture
def record_pipeline_version_execution_metadata(
    execution_metadata: Callable[[str, str | int], None],
    request: pytest.FixtureRequest,
) -> None:
    pipeline_path: Path = Path(request.config.rootpath) / ".github" / "workflows" / "charm-testing.yaml"

    # Get repository commit hash
    repository_version_command = ["git", "--no-pager", "log", "-n", "1", "--pretty=format:%h"]
    repository_result = run(repository_version_command, capture_output=True, text=True)  # nosec B603
    if repository_result.returncode == 0:
        execution_metadata("pipeline:ref", repository_result.stdout.strip())
    else:
        warnings.warn(f"Failed to get git commit hash: {repository_result.stderr.strip()}")

    # Get repository tag if it exists
    repository_tag_command = ["git", "describe", "--tags", "--exact-match", repository_result.stdout.strip()]
    repository_tag_result = run(repository_tag_command, capture_output=True, text=True)  # nosec B603
    if repository_tag_result.returncode == 0:
        execution_metadata("pipeline:tag", repository_tag_result.stdout.strip())
    elif "no tag exactly matches" in repository_tag_result.stderr.lower():
        warnings.warn("No tag exists in git repo pointing to this commit.")
    else:
        warnings.warn(f"Failed to get git tag: {repository_tag_result.stderr.strip()}")

    # Get pipeline workflow hash if file exists
    if pipeline_path.exists():
        pipeline_version_command = [
            "git",
            "hash-object",
            "--",
            str(pipeline_path.resolve()),
        ]
        pipeline_result = run(pipeline_version_command, capture_output=True, text=True)  # nosec B603
        if pipeline_result.returncode == 0:
            execution_metadata("pipeline:workflow_hash", pipeline_result.stdout.strip())
        else:
            warnings.warn(f"Failed to get pipeline workflow hash: {pipeline_result.stderr.strip()}")
    else:
        warnings.warn(f"Pipeline file not found: {pipeline_path}")


@pytest.fixture
def _kubernetes_test(juju_backend: JujuCmdBackend, model: str) -> None:
    if not juju_backend.is_k8s_model(model):
        pytest.skip("Not kubernetes")


@pytest.fixture
def kubernetes_client(_kubernetes_test: None) -> KubernetesClient:
    kubeconfig = os.environ.get("KUBECONFIG")
    return KubernetesClient(KubernetesBackend.k8s_client(kubeconfig=kubeconfig))
