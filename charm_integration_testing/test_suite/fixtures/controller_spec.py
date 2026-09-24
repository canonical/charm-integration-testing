# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from pathlib import Path

import pytest
from juju import JujuModelHandle
from pydantic import TypeAdapter, ValidationError
from utils import generate_juju_name

from test_suite.scheduler.states import STATES_WITHOUT_EXISTING_CONTROLLER, STATES_WITHOUT_EXISTING_MODEL, State


def needs_same_controller_cloud_registration(
    *,
    is_cmr_test: bool,
    same_controller: bool,
    neighbor_cloud: str | None,
    target_cloud: str,
) -> bool:
    """True when the neighbor cloud must be registered on the target controller.

    Same-controller mode (SQT-884: different platforms, same controller) shares a
    single controller between the target and neighbor models. If the neighbor lives on
    a different cloud than the target, that cloud is not yet known to the controller
    and must be registered (via ``JujuClient.add_cloud``) before a neighbor model can
    be created on it. Used both right after a fresh bootstrap
    (``test_bootstrap_controller``) and, when ``--current-state`` reuses a pre-existing
    controller, before the first test that needs the cloud
    (``register_preexisting_neighbor_cloud`` in ``conftest.py``).
    """
    return is_cmr_test and same_controller and neighbor_cloud != target_cloud


def _validate_cmr_options(
    config: pytest.Config,
    *,
    is_cmr: bool,
    same_controller: bool,
    target_controller: str | None,
    target_model: str | None,
    neighbor_controller: str | None,
    neighbor_model: str | None,
) -> None:
    if same_controller and not is_cmr:
        pytest.exit(
            "--same-controller requires --neighbor-cloud: CMR always needs a neighbor cloud "
            "(same-controller mode places the neighbor model on that cloud without "
            "bootstrapping a second controller for it).",
            returncode=4,
        )

    if same_controller and neighbor_controller is not None:
        pytest.exit(
            "--neighbor-controller must not be provided with --same-controller: the neighbor "
            "model shares the target controller.",
            returncode=4,
        )

    if (neighbor_controller or neighbor_model) and not is_cmr:
        pytest.exit(
            "--neighbor-cloud is required when providing --neighbor-controller or --neighbor-model.",
            returncode=4,
        )

    if not is_cmr:
        neighbor_config_opts = [
            "--neighbor-model-config",
            "--neighbor-controller-bootstrap-constraints",
            "--neighbor-controller-bootstrap-config",
            "--neighbor-controller-bootstrap-metadata-source",
        ]
        spurious = [opt for opt in neighbor_config_opts if config.getoption(opt, default=None)]
        if spurious:
            pytest.exit(
                f"Neighbor config options require --neighbor-cloud. Spurious options: {', '.join(spurious)}",
                returncode=4,
            )

    if is_cmr:
        if (
            neighbor_controller is not None
            and target_controller is not None
            and neighbor_model is not None
            and target_model is not None
            and neighbor_controller == target_controller
            and neighbor_model == target_model
        ):
            pytest.exit(
                f"--neighbor-controller and --neighbor-model must not be the same as "
                f"--target-controller and --target-model (got '{neighbor_controller}:{neighbor_model}'). "
                "CMR requires two distinct Juju models.",
                returncode=4,
            )
        if (
            same_controller
            and neighbor_model is not None
            and target_model is not None
            and neighbor_model == target_model
        ):
            pytest.exit(
                f"--neighbor-model must not be the same as --target-model (got '{neighbor_model}'). "
                "CMR requires two distinct Juju models.",
                returncode=4,
            )


def _validate_current_state_options(
    config: pytest.Config,
    *,
    is_cmr: bool,
    same_controller: bool,
    neighbor_controller: str | None,
    neighbor_model: str | None,
) -> None:
    raw_state = config.getoption("--current-state", default=None)
    if raw_state:
        try:
            current_state = State(raw_state)
        except ValueError:
            return  # Invalid value handled by scheduler plugin.
        if current_state not in STATES_WITHOUT_EXISTING_CONTROLLER and not config.getoption(
            "--target-controller", default=None
        ):
            pytest.exit(
                f"--target-controller is required when --current-state={current_state.value}.",
                returncode=4,
            )
        if current_state not in STATES_WITHOUT_EXISTING_MODEL and not config.getoption("--target-model", default=None):
            pytest.exit(
                f"--target-model is required when --current-state={current_state.value}.",
                returncode=4,
            )
        if (
            is_cmr
            and not same_controller
            and current_state not in STATES_WITHOUT_EXISTING_CONTROLLER
            and not neighbor_controller
        ):
            pytest.exit(
                f"--neighbor-controller is required when --current-state={current_state.value} with CMR.",
                returncode=4,
            )
        if is_cmr and current_state not in STATES_WITHOUT_EXISTING_MODEL and not neighbor_model:
            pytest.exit(
                f"--neighbor-model is required when --current-state={current_state.value} with CMR.",
                returncode=4,
            )


def pytest_configure(config: pytest.Config) -> None:
    target_controller = config.getoption("--target-controller", default=None)
    target_model = config.getoption("--target-model", default=None)
    neighbor_cloud = config.getoption("--neighbor-cloud", default=None)
    neighbor_controller = config.getoption("--neighbor-controller", default=None)
    neighbor_model = config.getoption("--neighbor-model", default=None)
    same_controller = config.getoption("--same-controller", default=False)
    is_cmr = neighbor_cloud is not None

    _validate_cmr_options(
        config,
        is_cmr=is_cmr,
        same_controller=same_controller,
        target_controller=target_controller,
        target_model=target_model,
        neighbor_controller=neighbor_controller,
        neighbor_model=neighbor_model,
    )
    _validate_current_state_options(
        config,
        is_cmr=is_cmr,
        same_controller=same_controller,
        neighbor_controller=neighbor_controller,
        neighbor_model=neighbor_model,
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--target-model", type=str, default=None, help="Juju model to test in.")
    parser.addoption(
        "--prefix",
        type=str,
        default=None,
        help="Prefix for auto-generated Juju controller and model names (e.g. 'charmqa-12345678' in CI).",
    )
    parser.addoption(
        "--target-cloud",
        type=str,
        default=None,
        help="Name of the Juju cloud to create the target controller on.",
    )
    parser.addoption(
        "--target-controller",
        type=str,
        default=None,
        help="Name of the Juju controller to create the target model on.",
    )
    parser.addoption(
        "--target-model-config",
        type=str,
        default=None,
        help="Path to a json file containing the model configurations to be passed down to Juju on model creation.",
    )
    parser.addoption(
        "--target-controller-bootstrap-constraints",
        type=str,
        default=None,
        help="Path to a json file containing the controller constraints configurations to be passed down to Juju on controller bootstrap.",
    )
    parser.addoption(
        "--target-controller-bootstrap-config",
        type=str,
        default=None,
        help="Path to a json file containing the controller bootstrap configurations to be passed down to Juju on controller bootstrap.",
    )
    parser.addoption(
        "--target-controller-bootstrap-metadata-source",
        type=str,
        default=None,
        help="Only used by Juju in OpenStack-based clouds. Path to the local folder where the metadata sources should be fetched and stored.",
    )
    parser.addoption(
        "--same-controller",
        action="store_true",
        default=False,
        help=(
            "Place the neighbor model (from --neighbor-cloud) on the target controller "
            "instead of bootstrapping a new one for it (same-controller CMR). Requires "
            "--neighbor-cloud; use the same cloud as --target-cloud for a same-platform "
            "neighbor, or a different cloud for a multi-cloud controller."
        ),
    )
    parser.addoption(
        "--neighbor-cloud",
        type=str,
        default=None,
        help="Juju cloud for the neighbor model's controller. Required for CMR tests.",
    )
    parser.addoption(
        "--neighbor-controller",
        type=str,
        default=None,
        help="Juju controller for the neighbor model. Required for CMR tests.",
    )
    parser.addoption(
        "--neighbor-model",
        type=str,
        default=None,
        help="Juju model name for the neighbor model. Required for CMR tests.",
    )
    parser.addoption(
        "--neighbor-model-config",
        type=str,
        default=None,
        help="Path to a json file containing the model configurations for the neighbor model.",
    )
    parser.addoption(
        "--neighbor-controller-bootstrap-constraints",
        type=str,
        default=None,
        help="Path to a json file containing the controller bootstrap constraints for the neighbor controller.",
    )
    parser.addoption(
        "--neighbor-controller-bootstrap-config",
        type=str,
        default=None,
        help="Path to a json file containing the controller bootstrap configuration for the neighbor controller.",
    )
    parser.addoption(
        "--neighbor-controller-bootstrap-metadata-source",
        type=str,
        default=None,
        help="Only used by Juju in OpenStack-based clouds. Path to the metadata sources folder for the neighbor controller.",
    )


def _load_json_config(option_name: str, value: str) -> dict[str, str]:
    ConfigSchema = TypeAdapter(dict[str, str])

    # Accept either an inline JSON object string or a path to a JSON file.
    value = value.strip()
    if value.startswith("{"):
        try:
            content = json.loads(value)
            return ConfigSchema.validate_python(content)
        except ValidationError as e:
            pytest.fail(f"Invalid config passed via {option_name}: {e}")
        except json.JSONDecodeError as e:
            pytest.fail(f"Inline JSON passed via {option_name} is not valid JSON: {e}")

    path = Path(value).resolve()
    if not path.exists() or not path.is_file():
        pytest.fail(f"File passed via {option_name} does not exist or is not a file.")

    try:
        content = json.loads(path.read_text())
        return ConfigSchema.validate_python(content)
    except ValidationError as e:
        pytest.fail(f"Invalid config passed via {option_name}: {e}")
    except json.JSONDecodeError as e:
        pytest.fail(f"File passed via {option_name} does not contain valid JSON: {e}")


@pytest.fixture(scope="session")
def prefix(request: pytest.FixtureRequest) -> str:
    """Prefix for auto-generated Juju resource names."""
    value = request.config.getoption("--prefix")
    if value:
        assert isinstance(value, str)
        return value
    return "charmqa"


@pytest.fixture(scope="session")
def model(request: pytest.FixtureRequest, prefix: str) -> str:
    value = request.config.getoption("--target-model")
    if value:
        assert isinstance(value, str)
        return value
    return generate_juju_name(prefix)


@pytest.fixture(scope="session")
def target_cloud(request: pytest.FixtureRequest) -> str:
    value = request.config.getoption("--target-cloud")
    if not value:
        pytest.fail("--target-cloud is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture(scope="session")
def target_controller(request: pytest.FixtureRequest, prefix: str) -> str:
    value = request.config.getoption("--target-controller")
    if value:
        assert isinstance(value, str)
        return value
    return generate_juju_name(prefix)


@pytest.fixture(scope="session")
def target_model_ref(target_controller: str, model: str) -> JujuModelHandle:
    """Explicit controller+model reference for the target model."""
    return JujuModelHandle(controller=target_controller, model=model)


@pytest.fixture
def target_model_config(request: pytest.FixtureRequest) -> dict[str, str]:
    """Juju model config for the target model, passed via ``--target-model-config``."""
    value = request.config.getoption("--target-model-config")
    if not value:
        return {}
    assert isinstance(value, str)
    return _load_json_config("--target-model-config", value)


@pytest.fixture
def target_controller_bootstrap_constraints(request: pytest.FixtureRequest) -> dict[str, str]:
    """Controller bootstrap constraints for the target controller, passed via ``--target-controller-bootstrap-constraints``."""
    value = request.config.getoption("--target-controller-bootstrap-constraints")
    if not value:
        return {}
    assert isinstance(value, str)
    return _load_json_config("--target-controller-bootstrap-constraints", value)


@pytest.fixture
def target_controller_bootstrap_config(request: pytest.FixtureRequest) -> dict[str, str]:
    """Controller bootstrap config for the target controller, passed via ``--target-controller-bootstrap-config``."""
    value = request.config.getoption("--target-controller-bootstrap-config")
    if not value:
        return {}
    assert isinstance(value, str)
    return _load_json_config("--target-controller-bootstrap-config", value)


@pytest.fixture
def target_controller_bootstrap_metadata_source(request: pytest.FixtureRequest) -> Path | None:
    """Controller bootstrap metadata source folder for the target controller, passed via ``--target-controller-bootstrap-metadata-source``.
    Only used in OpenStack-based deployments."""
    value = request.config.getoption("--target-controller-bootstrap-metadata-source")
    if not value:
        return None
    assert isinstance(value, str)
    path = Path(value).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def same_controller(request: pytest.FixtureRequest) -> bool:
    """True when the neighbor model shares the target controller (``--same-controller``)."""
    return bool(request.config.getoption("--same-controller"))


@pytest.fixture(scope="session")
def is_cmr_test(request: pytest.FixtureRequest) -> bool:
    """True when a cross-model relation test is configured (``--neighbor-cloud``).

    ``--same-controller`` only changes where that neighbor cloud's model is created
    (on the target controller instead of a new one); it never enables CMR by itself
    (enforced by ``pytest_configure``).
    """
    return bool(request.config.getoption("--neighbor-cloud"))


@pytest.fixture(scope="session")
def neighbor_cloud(request: pytest.FixtureRequest, is_cmr_test: bool) -> str | None:
    """Juju cloud for the neighbor model. Returns ``None`` in non-CMR tests."""
    if not is_cmr_test:
        return None
    value = request.config.getoption("--neighbor-cloud")
    assert isinstance(value, str)
    return value


@pytest.fixture(scope="session")
def neighbor_controller(
    request: pytest.FixtureRequest, is_cmr_test: bool, same_controller: bool, target_controller: str, prefix: str
) -> str | None:
    """Juju controller for the neighbor model. Returns ``None`` in non-CMR tests.

    In same-controller mode the neighbor model shares the target controller.
    """
    if not is_cmr_test:
        return None
    if same_controller:
        return target_controller
    value = request.config.getoption("--neighbor-controller")
    if value:
        assert isinstance(value, str)
        return value
    return generate_juju_name(prefix)


@pytest.fixture(scope="session")
def neighbor_model(request: pytest.FixtureRequest, is_cmr_test: bool, prefix: str) -> str | None:
    """Juju model name for the neighbor model. Returns ``None`` in non-CMR tests."""
    if not is_cmr_test:
        return None
    value = request.config.getoption("--neighbor-model")
    if value:
        assert isinstance(value, str)
        return value
    return generate_juju_name(prefix)


@pytest.fixture(scope="session")
def neighbor_model_ref(neighbor_controller: str | None, neighbor_model: str | None) -> JujuModelHandle | None:
    """Explicit controller+model reference for the neighbor model. ``None`` in non-CMR tests."""
    if neighbor_controller is None or neighbor_model is None:
        return None
    return JujuModelHandle(controller=neighbor_controller, model=neighbor_model)


@pytest.fixture
def neighbor_model_config(request: pytest.FixtureRequest, is_cmr_test: bool) -> dict[str, str] | None:
    """Juju model config for the neighbor model, passed via ``--neighbor-model-config``.
    Returns ``None`` in non-CMR tests (when neighbor routing options are not set)."""
    if not is_cmr_test:
        return None
    value = request.config.getoption("--neighbor-model-config")
    if not value:
        return {}
    assert isinstance(value, str)
    return _load_json_config("--neighbor-model-config", value)


@pytest.fixture
def neighbor_controller_bootstrap_constraints(
    request: pytest.FixtureRequest, is_cmr_test: bool
) -> dict[str, str] | None:
    """Controller bootstrap constraints for the neighbor controller, passed via ``--neighbor-controller-bootstrap-constraints``.
    Returns ``None`` in non-CMR tests (when neighbor routing options are not set)."""
    if not is_cmr_test:
        return None
    value = request.config.getoption("--neighbor-controller-bootstrap-constraints")
    if not value:
        return {}
    assert isinstance(value, str)
    return _load_json_config("--neighbor-controller-bootstrap-constraints", value)


@pytest.fixture
def neighbor_controller_bootstrap_config(request: pytest.FixtureRequest, is_cmr_test: bool) -> dict[str, str] | None:
    """Controller bootstrap configuration for the neighbor controller, passed via ``--neighbor-controller-bootstrap-config``.
    Returns ``None`` in non-CMR tests (when neighbor routing options are not set)."""
    if not is_cmr_test:
        return None
    value = request.config.getoption("--neighbor-controller-bootstrap-config")
    if not value:
        return {}
    assert isinstance(value, str)
    return _load_json_config("--neighbor-controller-bootstrap-config", value)


@pytest.fixture
def neighbor_controller_bootstrap_metadata_source(request: pytest.FixtureRequest, is_cmr_test: bool) -> Path | None:
    """Controller bootstrap metadata source folder for the neighbor controller, passed via ``--neighbor-controller-bootstrap-metadata-source``.
    Returns ``None`` in non-CMR tests (when neighbor routing options are not set)."""
    if not is_cmr_test:
        return None
    value = request.config.getoption("--neighbor-controller-bootstrap-metadata-source")
    if not value:
        return None
    assert isinstance(value, str)
    path = Path(value).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path
