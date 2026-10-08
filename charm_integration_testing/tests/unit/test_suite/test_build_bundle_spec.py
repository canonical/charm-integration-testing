# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

from test_suite.bundle_specs import application_specs

from bundle_builder_x import Charm, CharmChannel


def test_local_target_base_does_not_constrain_neighbor_base() -> None:
    target = Charm(
        name="my-charm",
        source_path=Path("/charms/my-charm"),
        ubuntu_version="26.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["machine"],
    )

    target_spec, neighbor_spec = application_specs(
        target_charm="my-charm",
        target_channel=None,
        target_revision=None,
        target_series=None,
        local_target_charm=Path("/charms/my-charm"),
        target_resolved_charm=target,
        neighbor_charm="neighbor",
    )

    assert target_spec.base == "26.04"
    assert neighbor_spec.base is None


def test_charmhub_target_keeps_neighbor_base_unset() -> None:
    target = Charm(
        name="my-charm",
        channel=CharmChannel.model_validate("1/stable"),
        revision=7,
        ubuntu_version="24.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["machine"],
    )

    target_spec, neighbor_spec = application_specs(
        target_charm="my-charm",
        target_channel="1/stable",
        target_revision=7,
        target_series="22.04",
        local_target_charm=None,
        target_resolved_charm=target,
        neighbor_charm="neighbor",
    )

    assert target_spec.base == "22.04"
    assert neighbor_spec.base is None
