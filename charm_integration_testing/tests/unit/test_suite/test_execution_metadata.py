# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from test_suite.conftest import _bundle_diagnostic_metadata, _release_resolution_metadata

from bundle_builder_x import (
    ApplicationReleaseDiagnostic,
    AssumesMismatchError,
    DiagnosticEndpoint,
    FeatureMismatchDiagnostic,
    PlatformMismatchError,
    ReleaseRequest,
    ReleaseUnavailableError,
    ReleaseUnavailableKind,
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

    entries = _release_resolution_metadata(error)

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

    entries = _release_resolution_metadata(error)

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

    entries = _release_resolution_metadata(aggregate)

    assert entries == {
        ("failure:build_bundle:release_resolution", "channel_not_found"),
        ("failure:build_bundle:release_resolution:architecture", "amd64"),
        ("failure:build_bundle:release_resolution:base", "22.04"),
        ("failure:build_bundle:release_resolution:channel", "latest/stable"),
        ("failure:build_bundle:release_resolution:charm", "aodh"),
        ("failure:build_bundle:release_resolution:error_code", "revision-not-found"),
    }


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
    assert all(value != "neighbor" for _, value in entries)
