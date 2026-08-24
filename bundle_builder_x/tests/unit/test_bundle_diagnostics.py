# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses

import pytest

from bundle_builder_x.bundle_builder import UncompletableBundleError
from bundle_builder_x.bundle_diagnostics import (
    BundleBuildFailureDiagnostic,
    BundleBuildFailureKind,
    DiagnosticEndpoint,
    UnfulfilledEndpointDiagnostic,
    UnresolvedApplicationDiagnostic,
    UnresolvedIntegrationDiagnostic,
    canonicalize_diagnostics,
)


def test_canonicalize_diagnostics_deduplicates_and_sorts() -> None:
    unresolved = UnresolvedApplicationDiagnostic(application="neighbor", charm_name="kafka")
    unfulfilled = UnfulfilledEndpointDiagnostic(
        endpoint=DiagnosticEndpoint(charm_name="postgresql", endpoint="db"),
        interface="pgsql",
    )

    result = canonicalize_diagnostics([unresolved, unfulfilled, unresolved])

    assert result == (unfulfilled, unresolved)


def test_diagnostics_are_immutable() -> None:
    diagnostic = UnresolvedApplicationDiagnostic(application="neighbor", charm_name="kafka")

    with pytest.raises(dataclasses.FrozenInstanceError):
        diagnostic.application = "changed"  # type: ignore[misc]


def test_error_renders_every_diagnostic_deterministically() -> None:
    unresolved = UnresolvedApplicationDiagnostic(application="neighbor", charm_name="kafka")
    unfulfilled = UnfulfilledEndpointDiagnostic(
        endpoint=DiagnosticEndpoint(charm_name="postgresql", endpoint="db"),
        interface="pgsql",
    )

    forward = UncompletableBundleError(diagnostics=[unresolved, unfulfilled])
    reversed_order = UncompletableBundleError(diagnostics=[unfulfilled, unresolved])

    assert str(forward) == str(reversed_order)
    assert "postgresql:db" in str(forward)
    assert "neighbor" in str(forward)
    assert "kafka" in str(forward)


def test_error_rejects_empty_diagnostics() -> None:
    with pytest.raises(ValueError, match="at least one diagnostic"):
        UncompletableBundleError(diagnostics=[])


def test_internal_failure_has_stable_kind() -> None:
    diagnostic = BundleBuildFailureDiagnostic(
        kind=BundleBuildFailureKind.SOLVER_TIMEOUT,
        detail="Solver timed out",
    )

    error = UncompletableBundleError(diagnostics=[diagnostic])

    assert error.diagnostics == (diagnostic,)
    assert diagnostic.kind is BundleBuildFailureKind.SOLVER_TIMEOUT


def test_unresolved_integration_sorts_endpoints() -> None:
    diagnostic = UnresolvedIntegrationDiagnostic(
        endpoints=(
            DiagnosticEndpoint(charm_name="zebra", endpoint="db", application="b"),
            DiagnosticEndpoint(charm_name="alpha", endpoint="client", application="a"),
        )
    )

    assert [endpoint.charm_name for endpoint in diagnostic.endpoints] == ["alpha", "zebra"]
