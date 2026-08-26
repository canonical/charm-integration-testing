# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from test_suite.conftest import _bundle_diagnostic_metadata, _release_resolution_metadata

from bundle_builder_x import (
    ApplicationReleaseDiagnostic,
    ArchitectureMismatchError,
    AssumesMismatchError,
    BaseMismatchError,
    CharmReleaseNotFoundException,
    DiagnosticEndpoint,
    FeatureMismatchDiagnostic,
    PeerChannelMismatchDiagnostic,
    PlatformMismatchError,
    ReleaseRequest,
    ReleaseUnavailableError,
    ReleaseUnavailableKind,
    SubordinateBaseMismatchDiagnostic,
    UnfulfilledEndpointDiagnostic,
    UnresolvedApplicationDiagnostic,
    UnresolvedIntegrationDiagnostic,
)


def test_platform_mismatch_metadata_uses_atomic_stable_values() -> None:
    error = PlatformMismatchError(
        request=ReleaseRequest(
            charm_name="aodh",
            architecture="amd64",
            platform="machine",
            channel="latest/stable",
        ),
        requested_platform="machine",
        supported_platforms=("kubernetes",),
    )

    entries = _release_resolution_metadata(error, "aodh")

    assert entries == {
        ("failure:build_bundle:release_resolution", "platform_mismatch"),
        ("failure:build_bundle:release_resolution:charm", "aodh"),
        ("failure:build_bundle:release_resolution:requested_platform", "machine"),
        ("failure:build_bundle:release_resolution:supported_platform", "kubernetes"),
    }


def test_assumes_metadata_emits_one_atomic_value_per_requirement() -> None:
    error = AssumesMismatchError(
        request=ReleaseRequest(
            charm_name="aodh",
            architecture="amd64",
            platform="machine",
            juju_version="3.5.0",
        ),
        unmet_requirements=("feature=unsupported-openstack", "juju>=3.6.0"),
        available_features=("juju",),
    )

    entries = _release_resolution_metadata(error, "aodh")

    assert entries == {
        ("failure:build_bundle:release_resolution", "assumes_mismatch"),
        ("failure:build_bundle:release_resolution:charm", "aodh"),
        ("failure:build_bundle:release_resolution:juju_version", "3.5.0"),
        ("failure:build_bundle:release_resolution:requirement", "feature=unsupported-openstack"),
        ("failure:build_bundle:release_resolution:requirement", "juju>=3.6.0"),
    }


def test_aggregate_release_metadata_flattens_and_deduplicates_children() -> None:
    child = ReleaseUnavailableError(
        kind=ReleaseUnavailableKind.CHANNEL_NOT_FOUND,
        request=ReleaseRequest(
            charm_name="aodh",
            architecture="amd64",
            base="22.04",
            channel="latest/stable",
        ),
        detail="free-form server message that must not be published",
        error_code="revision-not-found",
    )
    aggregate = ReleaseUnavailableError(
        kind=ReleaseUnavailableKind.TRACK_NOT_FOUND,
        request=ReleaseRequest(charm_name="aodh", track="latest"),
        detail="no risk worked",
        causes=(child, child),
    )

    entries = _release_resolution_metadata(aggregate, "aodh")

    assert entries == {
        ("failure:build_bundle:release_resolution", "channel_not_found"),
        ("failure:build_bundle:release_resolution:architecture", "amd64"),
        ("failure:build_bundle:release_resolution:base", "22.04"),
        ("failure:build_bundle:release_resolution:channel", "latest/stable"),
        ("failure:build_bundle:release_resolution:charm", "aodh"),
        ("failure:build_bundle:release_resolution:error_code", "revision-not-found"),
    }


def test_base_mismatch_metadata_uses_atomic_stable_values() -> None:
    error = BaseMismatchError(
        request=ReleaseRequest(charm_name="aodh", architecture="amd64"),
        requested_base="24.04",
        supported_bases=("22.04",),
    )

    entries = _release_resolution_metadata(error, "aodh")

    assert entries == {
        ("failure:build_bundle:release_resolution", "base_mismatch"),
        ("failure:build_bundle:release_resolution:charm", "aodh"),
        ("failure:build_bundle:release_resolution:requested_base", "24.04"),
        ("failure:build_bundle:release_resolution:supported_base", "22.04"),
        ("failure:build_bundle:release_resolution:architecture", "amd64"),
    }


def test_architecture_mismatch_metadata_uses_atomic_stable_values() -> None:
    error = ArchitectureMismatchError(
        request=ReleaseRequest(charm_name="aodh", architecture="arm64"),
        supported_architectures=("amd64",),
    )

    entries = _release_resolution_metadata(error, "aodh")

    assert entries == {
        ("failure:build_bundle:release_resolution", "architecture_mismatch"),
        ("failure:build_bundle:release_resolution:charm", "aodh"),
        ("failure:build_bundle:release_resolution:requested_architecture", "arm64"),
        ("failure:build_bundle:release_resolution:supported_architecture", "amd64"),
    }


@pytest.mark.parametrize(
    ("kind", "request_kwargs", "expected_extra_entries"),
    [
        (
            ReleaseUnavailableKind.MISSING_BASES,
            {"channel": "latest/stable", "revision": 7},
            {
                ("failure:build_bundle:release_resolution:channel", "latest/stable"),
                ("failure:build_bundle:release_resolution:revision", "7"),
            },
        ),
        (
            ReleaseUnavailableKind.CHANNEL_BASE_UNSUPPORTED,
            {"architecture": "amd64", "base": "22.04", "channel": "latest/stable", "revision": 7},
            {
                ("failure:build_bundle:release_resolution:architecture", "amd64"),
                ("failure:build_bundle:release_resolution:base", "22.04"),
                ("failure:build_bundle:release_resolution:channel", "latest/stable"),
                ("failure:build_bundle:release_resolution:revision", "7"),
            },
        ),
        (
            ReleaseUnavailableKind.REVISION_NOT_FOUND,
            {"revision": 7},
            {("failure:build_bundle:release_resolution:revision", "7")},
        ),
        (
            ReleaseUnavailableKind.NO_SUITABLE_CHANNEL,
            {"architecture": "amd64", "base": "22.04", "revision": 7},
            {
                ("failure:build_bundle:release_resolution:architecture", "amd64"),
                ("failure:build_bundle:release_resolution:base", "22.04"),
                ("failure:build_bundle:release_resolution:revision", "7"),
            },
        ),
        (
            ReleaseUnavailableKind.CHANNEL_NOT_FOUND,
            {"architecture": "amd64", "base": "22.04", "channel": "latest/stable"},
            {
                ("failure:build_bundle:release_resolution:architecture", "amd64"),
                ("failure:build_bundle:release_resolution:base", "22.04"),
                ("failure:build_bundle:release_resolution:channel", "latest/stable"),
            },
        ),
        (
            ReleaseUnavailableKind.TRACK_NOT_FOUND,
            {"architecture": "amd64", "base": "22.04", "track": "latest", "revision": 7},
            {
                ("failure:build_bundle:release_resolution:architecture", "amd64"),
                ("failure:build_bundle:release_resolution:base", "22.04"),
                ("failure:build_bundle:release_resolution:track", "latest"),
                ("failure:build_bundle:release_resolution:revision", "7"),
            },
        ),
        (
            ReleaseUnavailableKind.DEFAULT_RELEASE_NOT_FOUND,
            {"architecture": "amd64", "base": "22.04"},
            {
                ("failure:build_bundle:release_resolution:architecture", "amd64"),
                ("failure:build_bundle:release_resolution:base", "22.04"),
            },
        ),
        (
            ReleaseUnavailableKind.UNEXPECTED_STORE_RESPONSE,
            {"architecture": "amd64"},
            {("failure:build_bundle:release_resolution:architecture", "amd64")},
        ),
    ],
)
def test_release_unavailable_metadata_uses_kind_specific_fields(
    kind: ReleaseUnavailableKind,
    request_kwargs: dict[str, str | int],
    expected_extra_entries: set[tuple[str, str]],
) -> None:
    error = ReleaseUnavailableError(
        kind=kind,
        request=ReleaseRequest(charm_name="aodh", **request_kwargs),  # type: ignore[arg-type]
        detail="server message",
        error_code="some-error-code",
    )

    entries = _release_resolution_metadata(error, "aodh")

    assert entries == {
        ("failure:build_bundle:release_resolution", kind.value),
        ("failure:build_bundle:release_resolution:charm", "aodh"),
        ("failure:build_bundle:release_resolution:error_code", "some-error-code"),
        *expected_extra_entries,
    }


def test_release_resolution_metadata_reports_charm_when_request_is_unset() -> None:
    error = CharmReleaseNotFoundException("release not found", request=None)

    entries = _release_resolution_metadata(error, "aodh")

    assert ("failure:build_bundle:release_resolution:charm", "aodh") in entries


def test_bundle_diagnostic_metadata_dispatches_every_public_variant() -> None:
    release_error = PlatformMismatchError(
        request=ReleaseRequest(charm_name="aodh", platform="machine"),
        requested_platform="machine",
        supported_platforms=("kubernetes",),
    )
    diagnostics = [
        ApplicationReleaseDiagnostic(
            application="neighbor",
            charm_name="aodh",
            model="m",
            error=release_error,
        ),
        UnresolvedApplicationDiagnostic(application="neighbor2", charm_name="kafka"),
        UnresolvedIntegrationDiagnostic(
            endpoints=(
                DiagnosticEndpoint(charm_name="easyrsa", endpoint="client", application="target"),
                DiagnosticEndpoint(
                    charm_name="kafka",
                    endpoint="trusted-certificate",
                    application="neighbor2",
                ),
            )
        ),
        UnfulfilledEndpointDiagnostic(
            endpoint=DiagnosticEndpoint(charm_name="postgresql", endpoint="db"),
            interface="postgresql_client",
        ),
        FeatureMismatchDiagnostic(
            requires=DiagnosticEndpoint(charm_name="katib-controller", endpoint="service"),
            provides=DiagnosticEndpoint(charm_name="kfp-viz", endpoint="info"),
            feature="katib-service",
        ),
        PeerChannelMismatchDiagnostic(
            charm_name="kfp-persistence",
            endpoint="kfp-api",
            peer_charm_name="kfp-viz",
            required_track="2.15",
            required_risk=None,
            required_channel=None,
            required_revision=None,
        ),
        SubordinateBaseMismatchDiagnostic(
            subordinate_charm_name="nrpe",
            subordinate_endpoint="general-info",
            principal_charm_name="postgresql",
            principal_endpoint="juju-info",
            subordinate_base="ubuntu@22.04",
            principal_base="ubuntu@24.04",
        ),
    ]

    entries = [entry for diagnostic in diagnostics for entry in _bundle_diagnostic_metadata(diagnostic)]

    assert ("failure:build_bundle:unresolved_application", "kafka") in entries
    assert (
        "failure:build_bundle:unresolved_integration",
        "easyrsa:client/kafka:trusted-certificate",
    ) in entries
    assert ("failure:build_bundle:unfulfilled_endpoint", "postgresql:db") in entries
    assert ("failure:build_bundle:unfulfilled_interface", "postgresql_client") in entries
    assert (
        "failure:build_bundle:feature_mismatch",
        "katib-controller:service/kfp-viz:info",
    ) in entries
    assert ("failure:build_bundle:feature_mismatch:feature", "katib-service") in entries
    assert ("failure:build_bundle:release_resolution", "platform_mismatch") in entries
    assert ("failure:build_bundle:release_resolution:charm", "aodh") in entries
    assert (
        "failure:build_bundle:peer_channel_mismatch",
        "kfp-persistence:kfp-api/kfp-viz",
    ) in entries
    assert ("failure:build_bundle:peer_channel_mismatch:required_track", "2.15") in entries
    assert (
        "failure:build_bundle:subordinate_base_mismatch",
        "nrpe:general-info/postgresql:juju-info",
    ) in entries
    assert ("failure:build_bundle:subordinate_base_mismatch:subordinate_base", "ubuntu@22.04") in entries
    assert ("failure:build_bundle:subordinate_base_mismatch:principal_base", "ubuntu@24.04") in entries
    assert all(value != "neighbor" for _, value in entries)


def test_peer_channel_mismatch_metadata_includes_required_channel_fields() -> None:
    diagnostic = PeerChannelMismatchDiagnostic(
        charm_name="kfp-persistence",
        endpoint="kfp-api",
        peer_charm_name="kfp-viz",
        required_track="2.15",
        required_risk=None,
        required_channel=None,
        required_revision=None,
    )

    entries = _bundle_diagnostic_metadata(diagnostic)

    assert entries == [
        ("failure:build_bundle:peer_channel_mismatch", "kfp-persistence:kfp-api/kfp-viz"),
        ("failure:build_bundle:peer_channel_mismatch:required_track", "2.15"),
    ]


def test_subordinate_base_mismatch_metadata() -> None:
    diagnostic = SubordinateBaseMismatchDiagnostic(
        subordinate_charm_name="nrpe",
        subordinate_endpoint="general-info",
        principal_charm_name="postgresql",
        principal_endpoint="juju-info",
        subordinate_base="ubuntu@22.04",
        principal_base="ubuntu@24.04",
    )

    entries = _bundle_diagnostic_metadata(diagnostic)

    assert entries == [
        ("failure:build_bundle:subordinate_base_mismatch", "nrpe:general-info/postgresql:juju-info"),
        ("failure:build_bundle:subordinate_base_mismatch:subordinate_base", "ubuntu@22.04"),
        ("failure:build_bundle:subordinate_base_mismatch:principal_base", "ubuntu@24.04"),
    ]
