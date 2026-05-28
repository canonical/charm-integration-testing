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
from extensions import (
    ConfigureLivepatchServerExtension,
    PostgresqlDatabaseReplicationExtension,
    PostgresqlK8sDatabaseReplicationExtension,
    S3IntegratorMinIOBackendExtension,
    UnsealVaultJujuExtension,
    UnsealVaultK8sJujuExtension,
    ValidatorInjectorExtension,
)
from juju import JujuBackend, JujuClient, JujuValidationError, JujuVersion, JujuWaitTimeoutError
from juju.resource_registry import (
    JujuControllerHandle,
    JujuCrashdumpCollector,
    JujuModelHandle,
    JujuResourceRegistryExtension,
)
from juju_jubilant import JubilantBackend
from kubernetes_client import KubernetesBackend, KubernetesClient
from pytest import StashKey
from resource_registry import ResourceRegistry, ResourceTeardownWarning
from test_observer_client import TestObserverClient as TestObserverAPIClient
from test_observer_client import TestObserverClientError
from utils import generate_juju_name, normalize_string, normalize_string_multiline
from utils.juju_releases import (
    fetch_stable_juju_versions,
    select_upgrade_target,
)

from bundle_builder_x import UncompletableBundleError
from test_suite.scheduler.states import STATES_WITHOUT_EXISTING_CONTROLLER, STATES_WITHOUT_EXISTING_MODEL, State

pytest_plugins = [
    "test_suite.scheduler.plugin",
    "test_suite.fixtures.controller_spec",
]

KNOWN_FAILURE_EXCEPTIONS = (
    JujuWaitTimeoutError,
    JujuValidationError,
    AssertionError,
)


@pytest.fixture
def test_observer_api() -> str:
    """Test Observer API base URL from environment."""
    value = os.environ.get("TEST_OBSERVER_API")
    if not value:
        pytest.skip("Test Observer API URL is not configured (TEST_OBSERVER_API).")
    return value.strip()


@pytest.fixture
def test_observer_token() -> str:
    """Test Observer API token from environment."""
    value = os.environ.get("TEST_OBSERVER_TOKEN")
    if not value:
        pytest.skip("Test Observer API token is not configured (TEST_OBSERVER_TOKEN).")
    return value.strip()


@pytest.fixture
def test_observer_client(
    logger: logging.Logger,
    test_observer_api: str,
    test_observer_token: str,
) -> Iterator[TestObserverAPIClient]:
    """Test Observer API client."""
    try:
        client = TestObserverAPIClient(
            logger=logger,
            api_url=test_observer_api,
            token=test_observer_token,
        )
    except TestObserverClientError as exc:
        pytest.skip(f"Test Observer API client is not configured properly: {exc}")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session")
def logger() -> logging.Logger:
    jubilant_logger = logging.getLogger("jubilant")
    jubilant_logger.setLevel(logging.WARNING)

    jubilant_logger_wait = logging.getLogger("jubilant.wait")
    jubilant_logger_wait.setLevel(logging.WARNING)

    return logging.getLogger()


@pytest.fixture
def juju_backend(kubernetes_client: KubernetesClient | None) -> JujuBackend:
    return JubilantBackend(kubernetes_client=kubernetes_client)


@pytest.fixture(scope="session")
def log_dir(request: pytest.FixtureRequest) -> Path | None:
    """Session-scoped log directory for resource registry output.

    Set via ``--log-dir``. Returns ``None`` when not provided; log collection
    is skipped but resource cleanup still runs.
    """
    value = request.config.getoption("--log-dir", default=None)
    if not value:
        return None
    assert isinstance(value, str)
    path = Path(value).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def kubeconfig_path() -> Path | None:
    raw = os.environ.get("KUBECONFIG")
    stripped = None if raw is None else raw.strip() or None
    return Path(stripped) if stripped is not None else None


@pytest.fixture(scope="session")
def session_resource_registry(
    log_dir: Path | None,
    logger: logging.Logger,
    kubeconfig_path: Path | None,
) -> Iterator[ResourceRegistry]:
    """Session-scoped resource registry for the main workflow controller."""
    registry = ResourceRegistry(
        global_collectors=[
            JujuCrashdumpCollector(logger, output_dir=log_dir, kubeconfig_path=kubeconfig_path),
        ],
        logger=logger,
    )
    try:
        yield registry
    finally:
        try:
            registry.teardown_all()
        except Exception as exc:
            warnings.warn(f"session_resource_registry teardown_all raised: {exc}", ResourceTeardownWarning)


@pytest.fixture(scope="session", autouse=True)
def register_preexisting_resources(
    request: pytest.FixtureRequest,
    session_resource_registry: ResourceRegistry,
    target_controller: str,
    model: str,
    is_cmr_test: bool,
    neighbor_controller: str | None,
    neighbor_model: str | None,
) -> None:
    """Register controllers/models that already exist when --current-state skips bootstrap/create_model."""
    current_state = State(request.config.getoption("--current-state"))

    if current_state in STATES_WITHOUT_EXISTING_CONTROLLER:
        return

    target_ctrl_handle = JujuControllerHandle(controller=target_controller)
    if not session_resource_registry.is_registered(target_ctrl_handle):
        session_resource_registry.register(handle=target_ctrl_handle, destroyer=None)

    if is_cmr_test and neighbor_controller is not None:
        neighbor_ctrl_handle = JujuControllerHandle(controller=neighbor_controller)
        if not session_resource_registry.is_registered(neighbor_ctrl_handle):
            session_resource_registry.register(handle=neighbor_ctrl_handle, destroyer=None)

    if current_state in STATES_WITHOUT_EXISTING_MODEL:
        return

    session_resource_registry.register(
        handle=JujuModelHandle(controller=target_controller, model=model),
        parent=target_ctrl_handle,
    )

    if is_cmr_test and neighbor_controller is not None and neighbor_model is not None:
        neighbor_ctrl_handle = JujuControllerHandle(controller=neighbor_controller)
        session_resource_registry.register(
            handle=JujuModelHandle(controller=neighbor_controller, model=neighbor_model),
            parent=neighbor_ctrl_handle,
        )


@pytest.fixture
def juju_client(
    juju_backend: JujuBackend,
    logger: logging.Logger,
    minio_client_file: Path | None,
    ubuntu_pro_token: str | None,
    uv_file: Path | None,
    validators_path: Path | None,
    session_resource_registry: ResourceRegistry,
) -> JujuClient:
    return JujuClient(
        juju_backend,
        logger,
        extensions=[
            ConfigureLivepatchServerExtension(juju_backend, logger, ubuntu_pro_token),
            PostgresqlDatabaseReplicationExtension(juju_backend, logger),
            PostgresqlK8sDatabaseReplicationExtension(juju_backend, logger),
            S3IntegratorMinIOBackendExtension(juju_backend, logger, minio_client_file),
            UnsealVaultJujuExtension(juju_backend, logger),
            UnsealVaultK8sJujuExtension(juju_backend, logger),
            ValidatorInjectorExtension(validators_path, juju_backend, logger, uv_file),
            JujuResourceRegistryExtension(juju_backend, session_resource_registry),
        ],
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--log-dir",
        type=str,
        default=None,
        help="Directory to write resource logs (e.g. crashdumps) to. When omitted, log collection is skipped but cleanup still runs.",
    )
    parser.addoption(
        "--target-bundle",
        type=str,
        default=None,
        help="File path for the target model's bundle YAML (generated by test_build_bundle or provided externally).",
    )
    parser.addoption(
        "--neighbor-bundle",
        type=str,
        default=None,
        help="File path for the neighbor model's bundle YAML. Required for CMR tests.",
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
        "--target-downgrade-revision",
        type=str,
        default="default",
        help="Revision to downgrade to (integer). Use 'default' to query Test Observer for a historical revision with a passing deploy.",
    )
    parser.addoption(
        "--target-series",
        type=str,
        default="default",
        help="Ubuntu base series for the charm under test, e.g. '22.04'. Use 'default' to let the builder decide.",
    )
    parser.addoption(
        "--target-platform",
        type=str,
        default="kubernetes",
        choices=["kubernetes", "machine"],
        help="Platform to deploy on: 'kubernetes' or 'machine' (default: 'kubernetes').",
    )
    parser.addoption(
        "--neighbor-platform",
        type=str,
        default=None,
        choices=["kubernetes", "machine"],
        help="Platform for the neighbor model in CMR tests: 'kubernetes' or 'machine'. Defaults to --target-platform value.",
    )
    parser.addoption(
        "--charm-overrides",
        type=str,
        default="./static/charm-overrides/",
        help="Path to the unified charm overrides directory used by bundle-builder-x.",
    )
    parser.addoption(
        "--juju-upgrade-target-version",
        type=str,
        default=None,
        help="Explicit Juju version to upgrade the controller to, e.g. '3.6.21'. "
        "When omitted, an upgrade target is resolved from GitHub, preferring stable patch releases "
        "in the current minor version before higher minor releases.",
    )


@pytest.fixture
def target_bundle(request: pytest.FixtureRequest) -> Path:
    """Path to the target model's bundle YAML. Defaults to ``generated-target-bundle.yaml``."""
    value = request.config.getoption("--target-bundle")
    if not value:
        path = Path(request.config.rootpath) / "generated-target-bundle.yaml"
    else:
        assert isinstance(value, str)
        path = Path(value).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def neighbor_bundle(request: pytest.FixtureRequest, is_cmr_test: bool) -> Path | None:
    """Path to the neighbor model's bundle YAML for CMR tests.

    Returns ``None`` when neighbor routing options are absent (non-CMR test).
    Defaults to ``generated-neighbor-bundle.yaml`` when not explicitly provided.
    """
    if not is_cmr_test:
        return None

    value = request.config.getoption("--neighbor-bundle")
    if not value:
        ppath = Path(request.config.rootpath) / "generated-neighbor-bundle.yaml"
    else:
        assert isinstance(value, str)
        ppath = Path(value).resolve()
    ppath.parent.mkdir(parents=True, exist_ok=True)
    return ppath


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
def target_downgrade_revision(request: pytest.FixtureRequest) -> int:
    """Revision to downgrade to for the charm under test.

    When ``--target-downgrade-revision`` is an explicit integer, that value is
    returned directly. When the value is ``"default"``, Test Observer is queried
    for a historical revision with a passing deploy for the target charm.
    """
    value = request.config.getoption("--target-downgrade-revision")
    if value != "default":
        return int(value)

    test_observer_client: TestObserverAPIClient = request.getfixturevalue("test_observer_client")
    target_charm: str = request.getfixturevalue("target_charm")
    target_channel: str | None = request.getfixturevalue("target_channel")
    target_revision: int | None = request.getfixturevalue("target_revision")

    if target_revision is None or target_channel is None:
        pytest.fail(
            "--target-revision and --target-channel must be provided for this test to select a historical revision."
        )

    parts = target_channel.split("/", maxsplit=1)
    track = parts[0]
    stage = parts[1] if len(parts) > 1 else "stable"

    try:
        previous_revision = test_observer_client.choose_historical_revision_with_passing_deploy(
            charm_name=target_charm,
            stage=stage,
            current_revision=target_revision,
            track=track,
        )
        if previous_revision is None:
            pytest.fail(
                "Unable to find a historical revision with a passing test_deploy result "
                f"for charm '{target_charm}' in channel '{target_channel}'."
            )
        return previous_revision
    except TestObserverClientError as exc:
        raise RuntimeError(f"Test Observer query failed: {exc}") from exc


@pytest.fixture
def target_series(request: pytest.FixtureRequest) -> str | None:
    """Ubuntu base series for the charm under test.

    Returns ``None`` when the value is ``"default"``, which tells
    ``CharmhubClient.charm_from_store`` to pick a series based on the charm metadata.
    """
    value = request.config.getoption("--target-series")
    return None if value == "default" else value


@pytest.fixture
def target_platform(request: pytest.FixtureRequest) -> str:
    value = request.config.getoption("--target-platform")
    if not value:
        pytest.fail("--target-platform is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture
def target_juju_version(juju_backend: JujuBackend, model: str) -> JujuVersion:
    return juju_backend.version(model)


@pytest.fixture
def neighbor_juju_version(juju_backend: JujuBackend, neighbor_model: str | None) -> JujuVersion | None:
    if neighbor_model is None:
        return None
    return juju_backend.version(neighbor_model)


@pytest.fixture
def juju_cli_version(juju_backend: JujuBackend) -> JujuVersion:
    """Juju version resolved from the installed Juju CLI."""
    return juju_backend.cli_version()


@pytest.fixture
def charm_overrides(request: pytest.FixtureRequest) -> Path:
    """Path to the unified charm overrides directory, passed via ``--charm-overrides``."""
    value = request.config.getoption("--charm-overrides")
    if not value:
        pytest.fail("--charm-overrides is required by this test but was not provided.")
    assert isinstance(value, str)
    ppath = Path(value).resolve()
    if not ppath.exists():
        pytest.fail(f"Provided path for --charm-overrides does not exist: {ppath}")
    return ppath


@pytest.fixture
def neighbor_platform(request: pytest.FixtureRequest, target_platform: str) -> str:
    """Platform for the neighbor model in CMR tests. Falls back to --target-platform."""
    value = request.config.getoption("--neighbor-platform")
    if not value:
        return target_platform
    assert isinstance(value, str)
    return value


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
    session_resource_registry: ResourceRegistry,
    record_execution_metadata: None,
    require_temp_juju_controller_alive: None,
) -> Iterator[None]:
    def _print_all_model_statuses() -> None:
        for handle in session_resource_registry.registered_handles():
            if isinstance(handle, JujuModelHandle):
                try:
                    juju_client.print_status(model=f"{handle.controller}:{handle.model}")
                except Exception:
                    logger.warning(f"Failed to print status for model '{handle.controller}:{handle.model}'")

    # Print starting state for all registered models
    _print_all_model_statuses()

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

    # Log ending state for all registered models
    _print_all_model_statuses()


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
    pass


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

    consumed_offers = juju_client.list_consumed_offers(model=model).keys()

    # Get all integrations and record them
    for integration in juju_client.list_integrations(model=model):
        # Skip cross-model integrations where one side is a remote SAAS entry
        # TODO: record CMRs in a future iteration
        if integration.provider.application not in applications or integration.requirer.application not in applications:
            continue
        # Record integration in format: provider:endpoint/interface/requirer:endpoint
        try:
            integration_str = (
                f"{applications[integration.provider.application].charm}:{integration.provider.endpoint}/"
                f"{integration.interface}/"
                f"{applications[integration.requirer.application].charm}:{integration.requirer.endpoint}"
            )
            execution_metadata("integration", integration_str)
        except KeyError as err:
            if consumed_offers.isdisjoint({integration.provider.application, integration.requirer.application}):
                raise KeyError("neither app nor consumed offer") from err

            # FIXME(@motjuste): not recording execution metadata for consumed offers
            #   either use the URL which does not have charm info,
            #   or do a second status-check for offering model to get that info,
            #   AND, only do it for **actually** integrated offers


@pytest.fixture
def record_charms_and_revisions_execution_metadata(
    juju_client: JujuClient,
    session_resource_registry: ResourceRegistry,
    execution_metadata: Callable[[str, str | int], None],
    require_temp_juju_controller_alive: None,
) -> Iterator[None]:
    def _record_all() -> None:
        for handle in session_resource_registry.registered_handles():
            if isinstance(handle, JujuModelHandle):
                record_charms_and_revisions_execution_metadata_instantaneous(
                    juju_client, f"{handle.controller}:{handle.model}", execution_metadata
                )

    # Save all charms and revisions at start of test
    _record_all()

    yield

    # Save all charms and revisions at end of test
    _record_all()


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
        elif isinstance(exc, UncompletableBundleError):
            for info in exc.unfulfilled_endpoints:
                execution_metadata("failure:build_bundle:unfulfilled_endpoint", f"{info.charm_name}:{info.endpoint}")
                if info.interface:
                    execution_metadata("failure:build_bundle:unfulfilled_interface", info.interface)

        if error_message in request.node.stash:
            # toggle expected failure flag
            execution_metadata("failure:expected", "false")
        else:
            execution_metadata("failure:expected", "true")


@pytest.fixture
def record_juju_execution_metadata(
    juju_client: JujuClient,
    session_resource_registry: ResourceRegistry,
    execution_metadata: Callable[[str, str | int], None],
    require_temp_juju_controller_alive: None,
) -> Iterator[None]:
    yield

    # Save Juju version for each registered model
    for handle in session_resource_registry.registered_handles():
        if isinstance(handle, JujuModelHandle):
            execution_metadata("juju:version", str(juju_client.version(f"{handle.controller}:{handle.model}")))


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
def _is_running_on_kubernetes(juju_backend: JujuBackend, model: str) -> None:
    if not juju_backend.is_k8s_model(model):
        pytest.skip("Not running on kubernetes.")


@pytest.fixture
def kubernetes_client(
    logger: logging.Logger,
    kubeconfig_path: Path | None,
) -> KubernetesClient | None:
    if kubeconfig_path:
        return KubernetesClient(KubernetesBackend.k8s_client(kubeconfig=kubeconfig_path), logger=logger)
    return None


@pytest.fixture(scope="function")
def temp_juju_controller(
    juju_client: JujuClient,
    target_cloud: str,
    target_controller_bootstrap_constraints: dict[str, str],
    target_controller_bootstrap_config: dict[str, str],
    target_controller_bootstrap_metadata_source: Path | None,
    prefix: str,
    logger: logging.Logger,
) -> Iterator[str]:
    temp_controller_name = generate_juju_name(prefix)
    logger.info(f"Creating temporary fixture controller '{temp_controller_name}'.")
    juju_client.bootstrap_controller(
        cloud=target_cloud,
        controller=temp_controller_name,
        controller_constraints=target_controller_bootstrap_constraints,
        bootstrap_configuration=target_controller_bootstrap_config,
        metadata_source=target_controller_bootstrap_metadata_source,
    )

    yield temp_controller_name
    logger.info(f"Destroying temporary fixture controller '{temp_controller_name}'.")
    juju_client.kill_controller(controller=temp_controller_name)


@pytest.fixture
def require_temp_juju_controller_alive(request: pytest.FixtureRequest) -> None:
    """Ensure temp_juju_controller is set up before the depending fixture.

    Any fixture that queries model state during its teardown should depend on
    this so that the temporary controller is still alive when it runs (LIFO).
    """
    if "temp_juju_controller" in request.fixturenames:
        request.getfixturevalue("temp_juju_controller")


@pytest.fixture(scope="function")
def juju_controller_at_version(
    juju_client: JujuClient,
    target_cloud: str,
    target_controller_bootstrap_constraints: dict[str, str],
    target_upgrade_version: JujuVersion,
    target_controller_bootstrap_config: dict[str, str],
    target_controller_bootstrap_metadata_source: Path | None,
    prefix: str,
    logger: logging.Logger,
) -> Iterator[str]:
    """Bootstrap a controller pinned to the upgrade target version."""
    temp_controller_name = generate_juju_name(prefix)
    agent_version = str(target_upgrade_version)
    logger.info(f"Bootstrapping controller '{temp_controller_name}' at Juju {agent_version}.")
    juju_client.bootstrap_controller(
        cloud=target_cloud,
        controller=temp_controller_name,
        controller_constraints=target_controller_bootstrap_constraints,
        agent_version=agent_version,
        bootstrap_configuration=target_controller_bootstrap_config,
        metadata_source=target_controller_bootstrap_metadata_source,
    )

    yield temp_controller_name


@pytest.fixture
def target_upgrade_version(
    juju_client: JujuClient,
    juju_cli_version: JujuVersion,
    target_controller: str,
    request: pytest.FixtureRequest,
    logger: logging.Logger,
) -> JujuVersion | None:
    """Resolve the Juju version the controller should be upgraded to.

    Uses ``--juju-upgrade-target-version`` if provided, otherwise queries
    the GitHub releases API for the latest stable release above the current
    controller version.

    Returns ``None`` when no upgrade target is available (the controller is
    already at or above the latest stable release). Tests that consume this
    fixture should treat ``None`` as "nothing to do" and skip the
    upgrade-specific test flow.

    Cross-major upgrades are not supported.  Both the explicit and
    auto-detection paths restrict the target to the same major version as
    the current controller.
    """
    explicit = request.config.getoption("--juju-upgrade-target-version")
    controller_model = f"{target_controller}:controller"
    current = juju_client.version(controller_model)
    logger.info(f"Current Juju controller version: {current} (CLI: {juju_cli_version})")

    if explicit:
        target = JujuVersion.parse(str(explicit))
        if target <= current:
            logger.info(f"Explicit target {target} is not higher than current {current}; no upgrade needed.")
            return None
        if target.major != current.major:
            logger.info(
                f"Explicit target {target} has a different major version than current {current}; "
                "cross-major upgrades are not supported."
            )
            return None
        if target.major != juju_cli_version.major:
            pytest.skip(
                f"Cannot upgrade to {target}: the installed Juju client ({juju_cli_version}) "
                f"can only bootstrap {juju_cli_version.major}.x controllers. "
                "Install a Juju client at the target major version first."
            )
        logger.info(f"Selected explicit upgrade target: {target}")
        return target

    try:
        available = fetch_stable_juju_versions()
    except Exception as exc:
        pytest.skip(f"Unable to fetch Juju releases from GitHub: {exc}")

    logger.info(f"Fetched {len(available)} stable Juju releases from GitHub.")
    target_version = select_upgrade_target(current, available, allow_higher_major=False)
    if target_version is None:
        logger.info(f"No upgrade target above current version {current}; controller is already up to date.")
        return None

    logger.info(f"Selected upgrade target: {target_version}")
    return target_version
