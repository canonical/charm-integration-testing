# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError


def pytest_configure(config: pytest.Config) -> None:
    cloud = config.getoption("--neighbor-cloud", default=None)
    controller = config.getoption("--neighbor-controller", default=None)
    model = config.getoption("--neighbor-model", default=None)

    routing_opts = [
        ("--neighbor-cloud", cloud),
        ("--neighbor-controller", controller),
        ("--neighbor-model", model),
    ]
    provided_routing = [opt for opt, val in routing_opts if val]
    missing_routing = [opt for opt, val in routing_opts if not val]
    is_cmr = len(provided_routing) == 3

    if provided_routing and not is_cmr:
        pytest.exit(
            f"CMR routing options must all be provided together. Missing: {', '.join(missing_routing)}",
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
                f"Neighbor config options require CMR routing options "
                f"(--neighbor-cloud, --neighbor-controller, --neighbor-model). "
                f"Spurious options: {', '.join(spurious)}",
                returncode=4,
            )
        return

    target_controller = config.getoption("--target-controller", default=None)
    target_model = config.getoption("--target-model", default=None)
    if controller == target_controller and model == target_model:
        pytest.exit(
            f"--neighbor-controller and --neighbor-model must not be the same as "
            f"--target-controller and --target-model (got '{controller}:{model}'). "
            "CMR requires two distinct Juju models.",
            returncode=4,
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--target-model", type=str, required=True, help="Juju model to test in.")
    parser.addoption(
        "--target-cloud",
        type=str,
        default=None,
        help="Name of the Juju cloud to create the target controller on.",
    )
    parser.addoption(
        "--target-controller",
        type=str,
        default="charmqa",
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


@pytest.fixture
def model(request: pytest.FixtureRequest) -> str:
    option = request.config.getoption("--target-model")
    assert isinstance(option, str)
    return option


@pytest.fixture
def target_cloud(request: pytest.FixtureRequest) -> str:
    value = request.config.getoption("--target-cloud")
    if not value:
        pytest.fail("--target-cloud is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


@pytest.fixture
def target_controller(request: pytest.FixtureRequest) -> str:
    value = request.config.getoption("--target-controller")
    if not value:
        pytest.fail("--target-controller is required by this test but was not provided.")
    assert isinstance(value, str)
    return value


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


@pytest.fixture
def is_cmr_test(request: pytest.FixtureRequest) -> bool:
    """True when neighbor routing options indicate a cross-model relation test."""
    return bool(
        request.config.getoption("--neighbor-cloud")
        or request.config.getoption("--neighbor-controller")
        or request.config.getoption("--neighbor-model")
    )


@pytest.fixture
def neighbor_cloud(request: pytest.FixtureRequest, is_cmr_test: bool) -> str | None:
    """Juju cloud for the neighbor model's controller. Returns ``None`` in non-CMR tests."""
    if not is_cmr_test:
        return None
    value = request.config.getoption("--neighbor-cloud")
    assert isinstance(value, str)
    return value


@pytest.fixture
def neighbor_controller(request: pytest.FixtureRequest, is_cmr_test: bool) -> str | None:
    """Juju controller for the neighbor model. Returns ``None`` in non-CMR tests."""
    if not is_cmr_test:
        return None
    value = request.config.getoption("--neighbor-controller")
    assert isinstance(value, str)
    return value


@pytest.fixture
def neighbor_model(request: pytest.FixtureRequest, is_cmr_test: bool) -> str | None:
    """Juju model name for the neighbor model. Returns ``None`` in non-CMR tests."""
    if not is_cmr_test:
        return None
    value = request.config.getoption("--neighbor-model")
    assert isinstance(value, str)
    return value


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
