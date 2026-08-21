# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from test_suite.conftest import _bundle_diagnostic_metadata, _release_resolution_metadata_values

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


def test_platform_mismatch_metadata_excludes_run_local_names() -> None:
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

    values = _release_resolution_metadata_values(error)

    assert values == {
        "architecture=amd64 channel=latest/stable kind=platform_mismatch platform=machine "
        "requested_platform=machine supported_platforms=kubernetes"
    }
    assert all("aodh" not in value for value in values)


def test_assumes_metadata_emits_one_stable_value_per_requirement() -> None:
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

    values = _release_resolution_metadata_values(error)

    assert values == {
        "architecture=amd64 available_features=juju juju_version=3.5.0 kind=assumes_mismatch "
        "platform=machine requirement=feature=unsupported-openstack",
        "architecture=amd64 available_features=juju juju_version=3.5.0 kind=assumes_mismatch "
        "platform=machine requirement=juju>=3.6.0",
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

    values = _release_resolution_metadata_values(aggregate)

    assert values == {
        "architecture=amd64 base=22.04 channel=latest/stable error_code=revision-not-found " "kind=channel_not_found"
    }
    assert all("server message" not in value and "aodh" not in value for value in values)


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
    release_values = [value for category, value in entries if category.endswith("release_resolution")]
    assert len(release_values) == 1
    assert "aodh" not in release_values[0]
    assert "neighbor" not in release_values[0]
