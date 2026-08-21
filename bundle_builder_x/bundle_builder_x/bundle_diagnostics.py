# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Iterable

from .release_errors import CharmReleaseNotFoundException, release_error_key


class BundleDiagnostic(ABC):
    """Structured explanation for one reason a bundle could not be completed."""

    order: ClassVar[int]

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
    order: ClassVar[int] = 0

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
    order: ClassVar[int] = 1

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
class UnresolvedApplicationDiagnostic(BundleDiagnostic):
    order: ClassVar[int] = 2

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
    order: ClassVar[int] = 3

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
    order: ClassVar[int] = 4

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
    order: ClassVar[int] = 5

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
    """Deduplicate and sort diagnostics by structured identity."""
    unique = {(type(diagnostic), diagnostic.identity): diagnostic for diagnostic in diagnostics}
    return tuple(
        sorted(
            unique.values(),
            key=lambda diagnostic: (diagnostic.order, diagnostic.identity),
        )
    )
