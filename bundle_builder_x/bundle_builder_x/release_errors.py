# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ReleaseUnavailableKind(str, Enum):
    """Stable classifications for release lookup failures."""

    MISSING_BASES = "missing_bases"
    CHANNEL_BASE_UNSUPPORTED = "channel_base_unsupported"
    REVISION_NOT_FOUND = "revision_not_found"
    NO_SUITABLE_CHANNEL = "no_suitable_channel"
    CHANNEL_NOT_FOUND = "channel_not_found"
    TRACK_NOT_FOUND = "track_not_found"
    DEFAULT_RELEASE_NOT_FOUND = "default_release_not_found"
    UNEXPECTED_STORE_RESPONSE = "unexpected_store_response"


@dataclass(frozen=True)
class ReleaseRequest:
    """Selectors used to resolve one charm release."""

    charm_name: str
    architecture: str | None = None
    platform: str | None = None
    juju_version: str | None = None
    base: str | None = None
    channel: str | None = None
    track: str | None = None
    risk: str | None = None
    revision: int | None = None


class CharmReleaseNotFoundException(Exception):
    """Base error for a charm release rejected by structured compatibility checks."""

    def __init__(
        self,
        message: str,
        legacy_message: str | None = None,
        *,
        request: ReleaseRequest | None = None,
    ) -> None:
        if legacy_message is not None:
            message = f"{message}: {legacy_message}"
        self.request = request
        super().__init__(message)


class PlatformMismatchError(CharmReleaseNotFoundException):
    """A release supports different platforms from the requested model."""

    def __init__(
        self,
        *,
        request: ReleaseRequest,
        requested_platform: str,
        supported_platforms: tuple[str, ...],
    ) -> None:
        self.requested_platform = requested_platform
        self.supported_platforms = tuple(sorted(supported_platforms))
        super().__init__(
            f"Charm {request.charm_name!r} supports platform(s) {list(self.supported_platforms)!r}, "
            f"but platform {requested_platform!r} was requested",
            request=request,
        )


class AssumesMismatchError(CharmReleaseNotFoundException):
    """A release's assumes expression is not satisfied by the target environment."""

    def __init__(
        self,
        *,
        request: ReleaseRequest,
        unmet_requirements: tuple[str, ...],
        available_features: tuple[str, ...],
    ) -> None:
        self.unmet_requirements = tuple(sorted(unmet_requirements))
        self.available_features = tuple(sorted(available_features))
        super().__init__(
            f"Charm {request.charm_name!r} does not satisfy assumes constraints; unmet requirement(s) "
            f"{list(self.unmet_requirements)!r} for Juju {request.juju_version!r} and "
            f"available features {list(self.available_features)!r}",
            request=request,
        )


class BaseMismatchError(CharmReleaseNotFoundException):
    """A release does not support the requested Ubuntu base."""

    def __init__(
        self,
        *,
        request: ReleaseRequest,
        requested_base: str,
        supported_bases: tuple[str, ...] = (),
    ) -> None:
        self.requested_base = requested_base
        self.supported_bases = tuple(sorted(supported_bases))
        detail = f"; supported bases are {list(self.supported_bases)!r}" if self.supported_bases else ""
        super().__init__(
            f"Charm {request.charm_name!r} does not support Ubuntu {requested_base!r} "
            f"for architecture {request.architecture!r}{detail}",
            request=request,
        )


class ArchitectureMismatchError(CharmReleaseNotFoundException):
    """No release/base supports the requested architecture."""

    def __init__(
        self,
        *,
        request: ReleaseRequest,
        supported_architectures: tuple[str, ...] = (),
    ) -> None:
        self.supported_architectures = tuple(sorted(supported_architectures))
        detail = (
            f"; supported architectures are {list(self.supported_architectures)!r}"
            if self.supported_architectures
            else ""
        )
        super().__init__(
            f"Charm {request.charm_name!r} does not support architecture " f"{request.architecture!r}{detail}",
            request=request,
        )


class ReleaseUnavailableError(CharmReleaseNotFoundException):
    """No release matched store selectors or store response constraints."""

    def __init__(
        self,
        *,
        kind: ReleaseUnavailableKind,
        request: ReleaseRequest,
        detail: str,
        error_code: str | None = None,
        causes: tuple[CharmReleaseNotFoundException, ...] = (),
    ) -> None:
        self.kind = kind
        self.error_code = error_code
        self.causes = causes
        super().__init__(detail, request=request)


def leaf_release_errors(
    error: CharmReleaseNotFoundException,
) -> tuple[CharmReleaseNotFoundException, ...]:
    """Flatten aggregate release errors into deterministic leaf errors."""
    if isinstance(error, ReleaseUnavailableError) and error.causes:
        leaves = [leaf for cause in error.causes for leaf in leaf_release_errors(cause)]
        return tuple(sorted(leaves, key=release_error_key))
    return (error,)


def release_error_key(error: CharmReleaseNotFoundException) -> tuple[Any, ...]:
    """Return a stable structured identity for a release error."""
    request = error.request
    request_key = (
        (
            request.architecture or "",
            request.platform or "",
            request.juju_version or "",
            request.base or "",
            request.channel or "",
            request.track or "",
            request.risk or "",
            str(request.revision) if request.revision is not None else "",
        )
        if request is not None
        else ()
    )
    detail: tuple[Any, ...]
    if isinstance(error, PlatformMismatchError):
        detail = (error.requested_platform, error.supported_platforms)
    elif isinstance(error, AssumesMismatchError):
        detail = (error.unmet_requirements, error.available_features)
    elif isinstance(error, BaseMismatchError):
        detail = (error.requested_base, error.supported_bases)
    elif isinstance(error, ArchitectureMismatchError):
        detail = (error.supported_architectures,)
    elif isinstance(error, ReleaseUnavailableError):
        detail = (
            error.kind.value,
            error.error_code or "",
            tuple(release_error_key(cause) for cause in error.causes),
        )
    else:
        detail = (str(error),)
    return (type(error).__name__, request_key, detail)
