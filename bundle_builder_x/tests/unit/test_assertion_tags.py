# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for assertion_tags.py."""

from bundle_builder_x.assertion_tags import (
    Assertions,
    AssertionTag,
    CharmEndpointPayload,
    IntegrationFeatureMismatchTag,
    SubordinateBaseMismatchTag,
)


class TestSubordinateBaseMismatchTag:
    """SubordinateBaseMismatchTag: encode/decode and kind."""

    def test_encode_decode_roundtrip(self) -> None:
        tag = SubordinateBaseMismatchTag(
            subordinate_charm_name="nrpe",
            subordinate_charm_id=3,
            subordinate_endpoint="general-info",
            principal_charm_name="ubuntu",
            principal_charm_id=1,
            principal_endpoint="juju-info",
            subordinate_base="24.04",
            principal_base="22.04",
        )
        decoded = AssertionTag.decode(tag.encode())
        assert isinstance(decoded, SubordinateBaseMismatchTag)
        assert decoded.subordinate_charm_name == "nrpe"
        assert decoded.subordinate_charm_id == 3
        assert decoded.subordinate_endpoint == "general-info"
        assert decoded.principal_charm_name == "ubuntu"
        assert decoded.principal_charm_id == 1
        assert decoded.principal_endpoint == "juju-info"
        assert decoded.subordinate_base == "24.04"
        assert decoded.principal_base == "22.04"

    def test_kind_is_subordinate_base_mismatch(self) -> None:
        tag = SubordinateBaseMismatchTag(
            subordinate_charm_name="nrpe",
            subordinate_charm_id=0,
            subordinate_endpoint="general-info",
            principal_charm_name="ubuntu",
            principal_charm_id=1,
            principal_endpoint="juju-info",
            subordinate_base="22.04",
            principal_base="24.04",
        )
        assert tag.kind == Assertions.SUBORDINATE_BASE_MISMATCH


class TestIntegrationFeatureMismatchTag:
    """IntegrationFeatureMismatchTag: encode/decode and kind."""

    def test_encode_decode_roundtrip(self) -> None:
        tag = IntegrationFeatureMismatchTag(
            requires=CharmEndpointPayload(charm_name="katib-controller", charm_id=0, endpoint="k8s-service-info"),
            provides=CharmEndpointPayload(charm_name="kfp-viz", charm_id=1, endpoint="kfp-viz"),
            feature="katib-service",
        )
        decoded = AssertionTag.decode(tag.encode())
        assert isinstance(decoded, IntegrationFeatureMismatchTag)
        assert decoded.requires == CharmEndpointPayload(
            charm_name="katib-controller", charm_id=0, endpoint="k8s-service-info"
        )
        assert decoded.provides == CharmEndpointPayload(charm_name="kfp-viz", charm_id=1, endpoint="kfp-viz")
        assert decoded.feature == "katib-service"

    def test_kind_is_integration_feature_mismatch(self) -> None:
        tag = IntegrationFeatureMismatchTag(
            requires=CharmEndpointPayload(charm_name="requirer", charm_id=0, endpoint="svc"),
            provides=CharmEndpointPayload(charm_name="provider", charm_id=1, endpoint="svc"),
            feature="tag",
        )
        assert tag.kind == Assertions.INTEGRATION_FEATURE_MISMATCH
