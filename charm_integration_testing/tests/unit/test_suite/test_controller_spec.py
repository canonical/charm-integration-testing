# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import inspect
from collections.abc import Callable
from typing import Any, cast

import pytest
from test_suite.fixtures import controller_spec


class ConfigStub:
    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    def getoption(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)


class RequestStub:
    def __init__(self, options: dict[str, Any]) -> None:
        self.config = ConfigStub(options)


def _fixture(name: str) -> Callable[..., Any]:
    return cast(Callable[..., Any], inspect.unwrap(getattr(controller_spec, name)))


def _request(**options: Any) -> pytest.FixtureRequest:
    return cast(pytest.FixtureRequest, RequestStub(options))


def test_same_controller_alone_enables_cmr() -> None:
    # GIVEN a run configured with --same-controller and no --neighbor-cloud
    request = _request(**{"--same-controller": True})

    # WHEN resolving the CMR fixtures
    same_controller = _fixture("same_controller")(request)
    is_cmr_test = _fixture("is_cmr_test")(request)

    # THEN the run is treated as a CMR test on a single controller
    assert same_controller is True
    assert is_cmr_test is True


def test_neighbor_cloud_alone_enables_cmr() -> None:
    # GIVEN a run configured with --neighbor-cloud and no --same-controller
    request = _request(**{"--neighbor-cloud": "lxd"})

    # WHEN resolving the CMR fixtures
    same_controller = _fixture("same_controller")(request)
    is_cmr_test = _fixture("is_cmr_test")(request)

    # THEN the run is treated as a cross-controller CMR test
    assert same_controller is False
    assert is_cmr_test is True


def test_no_cmr_options_is_not_cmr() -> None:
    # GIVEN a run with neither CMR option
    request = _request()

    # WHEN resolving the CMR fixtures
    # THEN the run is not a CMR test
    assert _fixture("is_cmr_test")(request) is False


def test_same_controller_reuses_target_controller() -> None:
    # GIVEN a same-controller run
    request = _request(**{"--same-controller": True})

    # WHEN resolving the neighbor controller
    neighbor_controller = _fixture("neighbor_controller")(
        request,
        is_cmr_test=True,
        same_controller=True,
        target_controller="charmqa-12345678",
        prefix="charmqa",
    )

    # THEN the neighbor model shares the target controller
    assert neighbor_controller == "charmqa-12345678"


def test_same_controller_falls_back_to_target_cloud() -> None:
    # GIVEN a same-controller run without an explicit neighbor cloud
    request = _request(**{"--same-controller": True})

    # WHEN resolving the neighbor cloud
    neighbor_cloud = _fixture("neighbor_cloud")(
        request,
        is_cmr_test=True,
        same_controller=True,
        target_cloud="target-cloud",
    )

    # THEN the neighbor model is placed on the target cloud
    assert neighbor_cloud == "target-cloud"


def test_same_controller_honours_explicit_neighbor_cloud() -> None:
    # GIVEN a same-controller run that also names a neighbor cloud (multi-cloud controller)
    request = _request(**{"--same-controller": True, "--neighbor-cloud": "k8s-cloud"})

    # WHEN resolving the neighbor cloud
    neighbor_cloud = _fixture("neighbor_cloud")(
        request,
        is_cmr_test=True,
        same_controller=True,
        target_cloud="target-cloud",
    )

    # THEN the explicit cloud wins
    assert neighbor_cloud == "k8s-cloud"


def test_cross_controller_generates_neighbor_controller() -> None:
    # GIVEN a cross-controller run without an explicit neighbor controller
    request = _request(**{"--neighbor-cloud": "lxd"})

    # WHEN resolving the neighbor controller
    neighbor_controller = _fixture("neighbor_controller")(
        request,
        is_cmr_test=True,
        same_controller=False,
        target_controller="charmqa-12345678",
        prefix="charmqa",
    )

    # THEN a distinct controller name is generated
    assert neighbor_controller != "charmqa-12345678"
    assert neighbor_controller.startswith("charmqa-")


def test_configure_rejects_neighbor_controller_with_same_controller() -> None:
    # GIVEN --same-controller combined with an explicit --neighbor-controller
    config = ConfigStub({"--same-controller": True, "--neighbor-controller": "other"})

    # WHEN validating the options
    # THEN the combination is rejected
    with pytest.raises(pytest.exit.Exception, match="--neighbor-controller must not be provided"):
        controller_spec.pytest_configure(cast(pytest.Config, config))


def test_configure_rejects_neighbor_model_without_cmr() -> None:
    # GIVEN --neighbor-model without any CMR option
    config = ConfigStub({"--neighbor-model": "neighbor"})

    # WHEN validating the options
    # THEN the option is rejected
    with pytest.raises(pytest.exit.Exception, match="--neighbor-cloud or --same-controller is required"):
        controller_spec.pytest_configure(cast(pytest.Config, config))


def test_configure_rejects_identical_same_controller_model() -> None:
    # GIVEN --same-controller reusing the target model name
    config = ConfigStub(
        {
            "--same-controller": True,
            "--target-model": "shared",
            "--neighbor-model": "shared",
        }
    )

    # WHEN validating the options
    # THEN the identical model pair is rejected
    with pytest.raises(pytest.exit.Exception, match="--neighbor-model must not be the same as --target-model"):
        controller_spec.pytest_configure(cast(pytest.Config, config))


def test_configure_accepts_same_controller_with_distinct_model() -> None:
    # GIVEN --same-controller with a distinct neighbor model
    config = ConfigStub(
        {
            "--same-controller": True,
            "--target-model": "target",
            "--neighbor-model": "neighbor",
        }
    )

    # WHEN validating the options
    # THEN validation passes
    controller_spec.pytest_configure(cast(pytest.Config, config))
