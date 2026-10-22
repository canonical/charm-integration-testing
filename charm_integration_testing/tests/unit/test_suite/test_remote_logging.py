# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from test_suite.remote_logging import (
    build_remote_logging_endpoints_by_controller,
    remote_logging_existing_state_warning,
)


def test_builds_distinct_target_and_neighbor_controller_mappings() -> None:
    assert build_remote_logging_endpoints_by_controller(
        "target-controller",
        "http://target.example/loki",
        "neighbor-controller",
        "http://neighbor.example/loki",
    ) == {
        "target-controller": "http://target.example/loki",
        "neighbor-controller": "http://neighbor.example/loki",
    }


def test_same_controller_uses_target_endpoint() -> None:
    assert build_remote_logging_endpoints_by_controller(
        "shared-controller",
        "http://target.example/loki",
        "shared-controller",
        "http://neighbor.example/loki",
    ) == {"shared-controller": "http://target.example/loki"}


def test_same_controller_uses_neighbor_endpoint_when_target_is_unset() -> None:
    assert build_remote_logging_endpoints_by_controller(
        "shared-controller",
        None,
        "shared-controller",
        "http://neighbor.example/loki",
    ) == {"shared-controller": "http://neighbor.example/loki"}


def test_omits_unconfigured_controllers() -> None:
    assert (
        build_remote_logging_endpoints_by_controller(
            "target-controller",
            None,
            "neighbor-controller",
            None,
        )
        == {}
    )


def test_does_not_use_neighbor_endpoint_for_an_unconfigured_target_controller() -> None:
    assert build_remote_logging_endpoints_by_controller(
        "target-controller",
        None,
        "neighbor-controller",
        "http://neighbor.example/loki",
    ) == {"neighbor-controller": "http://neighbor.example/loki"}


def test_warns_when_remote_logging_reuses_an_existing_controller() -> None:
    assert remote_logging_existing_state_warning(
        True,
        controller_already_exists=True,
    ) == (
        "Remote logging is not initialized completely when --current-state reuses an existing controller; "
        "controller logs and pre-existing model logs may be absent."
    )


def test_does_not_warn_for_new_or_unconfigured_controllers() -> None:
    assert (
        remote_logging_existing_state_warning(
            True,
            controller_already_exists=False,
        )
        is None
    )
    assert remote_logging_existing_state_warning(False, controller_already_exists=True) is None
