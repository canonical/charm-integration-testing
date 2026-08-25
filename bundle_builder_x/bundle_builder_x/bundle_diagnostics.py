# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .release_errors import CharmReleaseNotFoundException, release_error_key


class BundleDiagnostic(ABC):
    """Structured explanation for one reason a bundle could not be completed."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Return a human-readable description."""

    @property
    @abstractmethod
    def identity(self) -> tuple[Any, ...]:
        """Return a stable identity used for sorting and deduplication."""


@dataclass(frozen=True)
class DiagnosticEndpoint:
    """Charm/application endpoint context without solver-internal identifiers."""

    charm_name: str
    endpoint: str
    application: str | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.charm_name, self.endpoint, self.application or "")


@dataclass(frozen=True)
class UnfulfilledEndpointDiagnostic(BundleDiagnostic):
    endpoint: DiagnosticEndpoint
    interface: str | None

    @property
    def description(self) -> str:
        return f"Cannot fulfill charm endpoint {self.endpoint.charm_name}:{self.endpoint.endpoint}"

    @property
    def identity(self) -> tuple[Any, ...]:
        return (*self.endpoint.identity, self.interface or "")


@dataclass(frozen=True)
class FeatureMismatchDiagnostic(BundleDiagnostic):
    requires: DiagnosticEndpoint
    provides: DiagnosticEndpoint
    feature: str

    @property
    def description(self) -> str:
        return (
            f"Charm endpoints {self.requires.charm_name}:{self.requires.endpoint} and "
            f"{self.provides.charm_name}:{self.provides.endpoint} have incompatible "
            f"feature {self.feature!r}"
        )

    @property
    def identity(self) -> tuple[Any, ...]:
        return (*self.requires.identity, *self.provides.identity, self.feature)


@dataclass(frozen=True)
class PeerChannelMismatchDiagnostic(BundleDiagnostic):
    """An endpoint requires its integration peer to be on a specific track/risk/channel/revision."""

    charm_name: str
    endpoint: str
    peer_charm_name: str
    required_track: str | None
    required_risk: str | None
    required_channel: str | None
    required_revision: int | None

    @property
    def description(self) -> str:
        requirement = self.required_channel or "/".join(
            part for part in (self.required_track, self.required_risk) if part
        )
        if self.required_revision is not None:
            requirement = (
                f"{requirement} (revision {self.required_revision})"
                if requirement
                else (f"revision {self.required_revision}")
            )
        return (
            f"Charm endpoint {self.charm_name}:{self.endpoint} requires its peer "
            f"{self.peer_charm_name} to be on channel {requirement}"
        )

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.charm_name,
            self.endpoint,
            self.peer_charm_name,
            self.required_track or "",
            self.required_risk or "",
            self.required_channel or "",
            self.required_revision if self.required_revision is not None else -1,
        )


@dataclass(frozen=True)
class SubordinateBaseMismatchDiagnostic(BundleDiagnostic):
    """A subordinate charm's endpoint requires the same base as its principal, but they differ."""

    subordinate_charm_name: str
    subordinate_endpoint: str
    principal_charm_name: str
    principal_endpoint: str
    subordinate_base: str
    principal_base: str

    @property
    def description(self) -> str:
        return (
            f"Subordinate endpoint {self.subordinate_charm_name}:{self.subordinate_endpoint} "
            f"(base {self.subordinate_base}) cannot attach to principal endpoint "
            f"{self.principal_charm_name}:{self.principal_endpoint} (base {self.principal_base})"
        )

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.subordinate_charm_name,
            self.subordinate_endpoint,
            self.principal_charm_name,
            self.principal_endpoint,
            self.subordinate_base,
            self.principal_base,
        )


@dataclass(frozen=True)
class UnresolvedApplicationDiagnostic(BundleDiagnostic):
    application: str
    charm_name: str

    @property
    def description(self) -> str:
        return f"Cannot resolve application {self.application} to charm {self.charm_name}"

    @property
    def identity(self) -> tuple[Any, ...]:
        return (self.application, self.charm_name)


@dataclass(frozen=True)
class UnresolvedIntegrationDiagnostic(BundleDiagnostic):
    endpoints: tuple[DiagnosticEndpoint, ...]

    def __post_init__(self) -> None:
        if not self.endpoints:
            raise ValueError("UnresolvedIntegrationDiagnostic requires at least one endpoint")
        object.__setattr__(self, "endpoints", tuple(sorted(self.endpoints, key=lambda endpoint: endpoint.identity)))

    @property
    def description(self) -> str:
        endpoints = "/".join(f"{endpoint.charm_name}:{endpoint.endpoint}" for endpoint in self.endpoints)
        return f"Cannot resolve integration {endpoints}"

    @property
    def identity(self) -> tuple[Any, ...]:
        return tuple(endpoint.identity for endpoint in self.endpoints)


@dataclass(frozen=True)
class ApplicationReleaseDiagnostic(BundleDiagnostic):
    application: str
    charm_name: str
    model: str
    error: CharmReleaseNotFoundException

    @property
    def description(self) -> str:
        return (
            f"Cannot resolve requested charm release for {self.application} "
            f"({self.charm_name}, model {self.model}): {self.error}"
        )

    @property
    def identity(self) -> tuple[Any, ...]:
        return (self.model, self.application, self.charm_name, release_error_key(self.error))


class BundleBuildFailureKind(str, Enum):
    """Stable classifications for failures internal to bundle construction."""

    EMPTY_UNSAT_CORE = "empty_unsat_core"
    SOLVER_TIMEOUT = "solver_timeout"
    UNEXPANDABLE_ASSERTIONS = "unexpandable_assertions"
    OPTIMIZATION_UNSATISFIABLE = "optimization_unsatisfiable"
    OPTIMIZATION_TIMEOUT = "optimization_timeout"


@dataclass(frozen=True)
class BundleBuildFailureDiagnostic(BundleDiagnostic):
    kind: BundleBuildFailureKind
    detail: str

    @property
    def description(self) -> str:
        return self.detail

    @property
    def identity(self) -> tuple[Any, ...]:
        return (self.kind.value, self.detail)


def canonicalize_diagnostics(
    diagnostics: Iterable[BundleDiagnostic],
) -> tuple[BundleDiagnostic, ...]:
    """Deduplicate and sort diagnostics by structured identity.

    Presentation order groups diagnostics by kind, in the order below, then by each
    kind's own structured identity.
    """
    diagnostic_order: tuple[type[BundleDiagnostic], ...] = (
        UnfulfilledEndpointDiagnostic,
        FeatureMismatchDiagnostic,
        PeerChannelMismatchDiagnostic,
        SubordinateBaseMismatchDiagnostic,
        UnresolvedApplicationDiagnostic,
        UnresolvedIntegrationDiagnostic,
        ApplicationReleaseDiagnostic,
        BundleBuildFailureDiagnostic,
    )
    unique = {(type(diagnostic), diagnostic.identity): diagnostic for diagnostic in diagnostics}
    return tuple(
        sorted(
            unique.values(),
            key=lambda diagnostic: (diagnostic_order.index(type(diagnostic)), diagnostic.identity),
        )
    )
