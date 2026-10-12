# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from test_suite.conftest import _build_remote_logging_endpoints_by_controller


def test_builds_distinct_target_and_neighbor_controller_mappings() -> None:
    assert _build_remote_logging_endpoints_by_controller(
        "target-controller",
        "target.example:4317",
        "neighbor-controller",
        "neighbor.example:4317",
    ) == {
        "target-controller": "target.example:4317",
        "neighbor-controller": "neighbor.example:4317",
    }


def test_same_controller_uses_target_endpoint() -> None:
    assert _build_remote_logging_endpoints_by_controller(
        "shared-controller",
        "target.example:4317",
        "shared-controller",
        "neighbor.example:4317",
    ) == {"shared-controller": "target.example:4317"}


def test_omits_unconfigured_controllers() -> None:
    assert (
        _build_remote_logging_endpoints_by_controller(
            "target-controller",
            None,
            "neighbor-controller",
            None,
        )
        == {}
    )
