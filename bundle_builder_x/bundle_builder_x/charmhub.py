# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import re
from typing import Any

from .charm import (
    ASSUMES_OPS,
    JUJU_INFO_INTERFACE,
    Charm,
    CharmAssumesEntry,
    CharmChannel,
    CharmConfigValue,
    CharmEndpoint,
    CharmResourceValue,
    EndpointScope,
    EndpointType,
)
from .charmhub_http import (
    CharmConfigSchema,
    CharmhubBase,
    CharmhubHttpClient,
    CharmMetadata,
    IncompleteCharmInfoException,
    RefreshAction,
    RefreshResponse,
    UnparsableCharmException,
)
from .constraints_dsl import AnyExpr, DSLType, parse_constraint
from .juju_version import JujuVersion
from .overrides import CharmEndpointOverrides, OverridesClient
from .release_errors import (
    ArchitectureMismatchError,
    AssumesMismatchError,
    BaseMismatchError,
    CharmReleaseNotFoundException,
    PlatformMismatchError,
    ReleaseRequest,
    ReleaseUnavailableError,
    ReleaseUnavailableKind,
)
from .timing import NullTimeline, Timeline

# Matches juju version constraint strings in charm assumes blocks e.g. "juju >= 3.0" or "juju>=3.0"
# Operators are sorted by descending length so longer ops (>=, <=, ==) are tried before
# their single-char prefixes (>, <) to avoid prefix shadowing in alternation.
# Whitespace around the operator is optional to support both "juju >= 3.0" and "juju>=3.0".
_ASSUMES_JUJU_RE = re.compile(
    rf"^juju\s*({'|'.join(re.escape(op) for op in sorted(ASSUMES_OPS, key=len, reverse=True))})\s*(\S+)$"
)

_PLATFORM_FEATURES: dict[str, frozenset[str]] = {
    "kubernetes": frozenset(["juju", "k8s-api"]),
    "machine": frozenset(["juju"]),
}


class CharmhubClient:
    logger: logging.Logger
    http_client: CharmhubHttpClient
    overrides_client: OverridesClient
    timeline: Timeline

    def __init__(
        self,
        http_client: CharmhubHttpClient | None = None,
        logger: logging.Logger | None = None,
        overrides_client: OverridesClient | None = None,
        timeline: Timeline | None = None,
    ) -> None:
        self.logger = logger.getChild("charmhub") if logger is not None else logging.getLogger(__name__)
        self.http_client = http_client if http_client is not None else CharmhubHttpClient(logger=self.logger)
        self.overrides_client = overrides_client if overrides_client is not None else OverridesClient()
        self.timeline = (timeline if timeline is not None else NullTimeline()).child("charmhub")

    def get_charm_channels(self, charm_name: str) -> list[CharmChannel]:
        """Return all published channels for a charm, sorted by track then risk tier (stable first)."""
        info = self.http_client.info(charm_name, include_channel_map=True)
        return sorted({CharmChannel.model_validate(entry.channel.name) for entry in info.channel_map})

    def charm_from_store(
        self,
        charm_name: str,
        ubuntu_arch: str,
        juju_version: JujuVersion | None = None,
        platform: str | None = None,
        charm_track: str | None = None,
        charm_risk: str | None = None,
        charm_revision: int | None = None,
        ubuntu_version: str | None = None,
    ) -> Charm:
        token = self.timeline.on(f"charm/{charm_name}")
        try:
            # Resolve track/risk/revision from overrides when none are specified by the caller.
            # Note: branch components in override channels are intentionally not supported.
            if charm_track is None and charm_risk is None and charm_revision is None:
                raw_channel = self.overrides_client.get_charm_default_channel(charm_name)
                if raw_channel is not None:
                    default_channel = CharmChannel.model_validate(raw_channel)
                    charm_track = default_channel.track or None
                    charm_risk = default_channel.risk or None
                charm_revision = self.overrides_client.get_charm_default_revision(charm_name)

            # Route to the appropriate fetch strategy.
            if charm_track is not None and charm_risk is not None and charm_revision is not None:
                result = self._charm_from_store_by_channel_and_revision(
                    charm_name=charm_name,
                    ubuntu_arch=ubuntu_arch,
                    juju_version=juju_version,
                    platform=platform,
                    charm_channel=CharmChannel(track=charm_track, risk=charm_risk, branch=""),
                    charm_revision=charm_revision,
                    ubuntu_version=ubuntu_version,
                )
            elif charm_track is not None and charm_revision is not None:
                result = self._charm_from_store_by_track_and_revision(
                    charm_name=charm_name,
                    ubuntu_arch=ubuntu_arch,
                    juju_version=juju_version,
                    platform=platform,
                    charm_track=charm_track,
                    charm_revision=charm_revision,
                    ubuntu_version=ubuntu_version,
                )
            elif charm_revision is not None:
                result = self._charm_from_store_by_revision(
                    charm_name=charm_name,
                    ubuntu_arch=ubuntu_arch,
                    juju_version=juju_version,
                    platform=platform,
                    charm_revision=charm_revision,
                    ubuntu_version=ubuntu_version,
                )
            elif charm_track is not None and charm_risk is not None:
                result = self._charm_from_store_by_channel(
                    charm_name=charm_name,
                    ubuntu_arch=ubuntu_arch,
                    juju_version=juju_version,
                    platform=platform,
                    charm_channel=CharmChannel(track=charm_track, risk=charm_risk, branch=""),
                    ubuntu_version=ubuntu_version,
                )
            elif charm_track is not None:
                result = self._charm_from_store_by_track(
                    charm_name=charm_name,
                    ubuntu_arch=ubuntu_arch,
                    juju_version=juju_version,
                    platform=platform,
                    charm_track=charm_track,
                    ubuntu_version=ubuntu_version,
                )
            elif charm_risk is not None:
                result = self._charm_from_store_by_channel(
                    charm_name=charm_name,
                    ubuntu_arch=ubuntu_arch,
                    juju_version=juju_version,
                    platform=platform,
                    charm_channel=CharmChannel(track="", risk=charm_risk, branch=""),
                    ubuntu_version=ubuntu_version,
                )
            else:
                result = self._charm_from_store_default(
                    charm_name=charm_name,
                    ubuntu_arch=ubuntu_arch,
                    juju_version=juju_version,
                    platform=platform,
                    ubuntu_version=ubuntu_version,
                )
        finally:
            self.timeline.off(token)
        return result

    def find_charms(
        self, provides: str | None = None, requires: str | None = None, platform: str | None = None
    ) -> set[str]:
        # juju-info is implicitly provided by every machine charm and is never declared
        # explicitly in most charm metadata. The charmhub find API returns 0 results for
        # provides=juju-info because principals don't publish that endpoint.
        # Dropping the filter and returning all charms (for the given platform) is the
        # correct behaviour: every machine charm is a valid juju-info provider.
        # Note: the natural optimisation would be to filter find results by subordinate=True
        # when looking for subordinates, but that field is broken in the find endpoint -
        # see LP#2140485 (default-release.revision.subordinate not returned by find).
        if provides == JUJU_INFO_INTERFACE:
            provides = None

        key = f"find/{provides or ''}/{requires or ''}"
        token = self.timeline.on(key)
        try:
            # Call find API
            response = self.http_client.find(provides=provides, requires=requires)

            # Map charms to deployable on
            charms = {charm.name: charm.result.deployable_on for charm in response}

            # Add charms with listing overrides
            for charm, platforms in self._find_charms_with_listing_overrides(
                provides=provides, requires=requires
            ).items():
                charms[charm] = platforms

            # Remove charms with delisting overrides
            delisted = self.overrides_client.get_charm_delisting_overrides()
            charms = {charm: platforms for charm, platforms in charms.items() if charm not in delisted}

            # Add platform overrides
            for charm, platforms in self._find_charms_platform_overrides(set(charms.keys())).items():
                charms[charm] = platforms

            # Default to machine if empty
            charms = {charm: platforms if len(platforms) > 0 else {"machine"} for charm, platforms in charms.items()}

            # Return response filtered by platform
            if platform is None:
                result = set(charms.keys())
            else:
                result = {charm for charm, platforms in charms.items() if platform in platforms}
        finally:
            self.timeline.off(token)
        return result

    def _find_charms_with_listing_overrides(
        self, provides: str | None = None, requires: str | None = None
    ) -> dict[str, set[str]]:
        # Get charm info for each listing overridden charm
        charms = {}
        for charm in self.overrides_client.get_charm_listing_overrides():
            # Get charm info
            charm_info = self.http_client.info(charm)

            # Filter results
            if provides is not None and provides not in {
                endpoint.interface for endpoint in charm_info.default_release.revision.metadata.provides.values()
            }:
                continue
            if requires is not None and requires not in {
                endpoint.interface for endpoint in charm_info.default_release.revision.metadata.requires.values()
            }:
                continue

            # Add find response
            charms[charm] = set(charm_info.result.deployable_on)

        return charms

    def _find_charms_platform_overrides(self, charms: set[str]) -> dict[str, set[str]]:
        overrides = {}
        for charm in charms:
            platform_overrides = self.overrides_client.get_charm_platform_overrides(charm)
            if platform_overrides is not None:
                overrides[charm] = set(platform_overrides)
        return overrides

    def _build_charm(
        self,
        charm_name: str,
        channel: CharmChannel,
        revision: int,
        ubuntu_version: str,
        ubuntu_arch: str,
        metadata: CharmMetadata,
        config_schema: CharmConfigSchema,
    ) -> Charm:
        return Charm(
            name=charm_name,
            channel=channel,
            revision=revision,
            ubuntu_version=ubuntu_version,
            ubuntu_arch=ubuntu_arch,
            subordinate=metadata.subordinate,
            endpoints=self._get_charm_endpoints(charm_name, metadata, channel),
            proxies=self.overrides_client.get_charm_proxy_overrides(charm_name, channel),
            priority=self.overrides_client.get_charm_priority(charm_name),
            configs=self._get_charm_configs(charm_name, channel, config_schema),
            config_defaults={k: v.default for k, v in config_schema.options.items()},
            resources=self._get_charm_resources(charm_name, channel, metadata),
            assumes=self._get_charm_assumes(charm_name, metadata, channel),
            constraints=self._get_charm_constraints(charm_name, channel),
            platforms=self._get_charm_platforms(charm_name, channel, metadata),
        )

    def _ensure_compatibility(self, charm: Charm, juju_version: JujuVersion | None, platform: str | None) -> Charm:
        request = ReleaseRequest(
            charm_name=charm.name,
            architecture=charm.ubuntu_arch,
            platform=platform,
            juju_version=str(juju_version) if juju_version is not None else None,
            base=charm.ubuntu_version,
            channel=str(charm.channel),
            revision=charm.revision,
        )
        if platform is not None:
            supported_platforms = charm.platforms
            if platform not in supported_platforms:
                raise PlatformMismatchError(
                    request=request,
                    requested_platform=platform,
                    supported_platforms=tuple(supported_platforms),
                )

        if juju_version is None and platform is None:
            return charm
        features = (
            _PLATFORM_FEATURES[platform]
            if platform is not None and platform in _PLATFORM_FEATURES
            else frozenset(["juju"])
        )
        if not charm.assumes.satisfied_by(juju_version, features):
            raise AssumesMismatchError(
                request=request,
                unmet_requirements=charm.assumes.unsatisfied_requirements(juju_version, features),
                available_features=tuple(features),
            )
        return charm

    def _charm_from_store_by_channel_and_revision(
        self,
        charm_name: str,
        ubuntu_arch: str,
        juju_version: JujuVersion | None,
        platform: str | None,
        charm_channel: CharmChannel,
        charm_revision: int,
        ubuntu_version: str | None = None,
    ) -> Charm:
        # Get refresh info for revision
        refresh_info = self._get_revision_refresh_info(charm_name, charm_revision)

        # Get or validate ubuntu version from bases
        if refresh_info.charm is None or refresh_info.charm.bases is None:
            raise ReleaseUnavailableError(
                kind=ReleaseUnavailableKind.MISSING_BASES,
                request=ReleaseRequest(
                    charm_name=charm_name,
                    architecture=ubuntu_arch,
                    platform=platform,
                    revision=charm_revision,
                    channel=str(charm_channel),
                ),
                detail=f"Charm {charm_name} revision {charm_revision} has no bases information",
            )
        ubuntu_version = self._get_ubuntu_version_from_bases(
            refresh_info.charm.bases, ubuntu_arch, charm_name, charm_revision, ubuntu_version
        )

        # Verify the channel supports the ubuntu version that this revision requires.
        # The Charmhub API rejects channel+revision together, so Juju resolves them
        # separately: revision for base compatibility, channel for future refreshes.
        # If the channel doesn't support the ubuntu version the revision needs,
        # the deployment will fail when Juju calls Charmhub with channel+base.
        if not self._channel_supports_ubuntu_version(charm_name, ubuntu_arch, ubuntu_version, charm_channel):
            raise ReleaseUnavailableError(
                kind=ReleaseUnavailableKind.CHANNEL_BASE_UNSUPPORTED,
                request=ReleaseRequest(
                    charm_name=charm_name,
                    architecture=ubuntu_arch,
                    platform=platform,
                    base=ubuntu_version,
                    channel=str(charm_channel),
                    revision=charm_revision,
                ),
                detail=(
                    f"Charm {charm_name} channel {charm_channel} does not support Ubuntu "
                    f"{ubuntu_version} for architecture {ubuntu_arch}"
                ),
            )

        # Return Charm from refresh info
        return self._ensure_compatibility(
            charm=self._build_charm(
                charm_name=charm_name,
                channel=charm_channel,
                revision=charm_revision,
                ubuntu_version=ubuntu_version,
                ubuntu_arch=ubuntu_arch,
                metadata=refresh_info.charm.metadata,
                config_schema=refresh_info.charm.config,
            ),
            juju_version=juju_version,
            platform=platform,
        )

    def _charm_from_store_by_revision(
        self,
        charm_name: str,
        ubuntu_arch: str,
        juju_version: JujuVersion | None,
        platform: str | None,
        charm_revision: int,
        ubuntu_version: str | None = None,
    ) -> Charm:
        # Get refresh info for revision
        refresh_info = self._get_revision_refresh_info(charm_name, charm_revision)

        # Check for errors and incomplete data
        if refresh_info.error is not None:
            raise ReleaseUnavailableError(
                kind=ReleaseUnavailableKind.REVISION_NOT_FOUND,
                request=ReleaseRequest(
                    charm_name=charm_name,
                    architecture=ubuntu_arch,
                    platform=platform,
                    revision=charm_revision,
                ),
                detail=f"Failed to find charm {charm_name} for revision {charm_revision}: {refresh_info.error.message}",
                error_code=refresh_info.error.code,
            )
        if refresh_info.charm is None:
            raise IncompleteCharmInfoException(
                f"Refresh info for charm {charm_name} revision {charm_revision} returned no charm and no error"
            )
        if refresh_info.charm.bases is None:
            raise IncompleteCharmInfoException(
                f"Refresh info for charm {charm_name} revision {charm_revision} returned no bases"
            )

        # Get or validate ubuntu version from bases
        ubuntu_version = self._get_ubuntu_version_from_bases(
            refresh_info.charm.bases, ubuntu_arch, charm_name, charm_revision, ubuntu_version
        )

        # Find suitable channel (must support base)
        default_refresh_info = self._default_refresh_info(
            charm_name,
            CharmhubBase(
                channel=ubuntu_version,
                architecture=ubuntu_arch,
            ),
        )

        # Return Charm from refresh info
        if default_refresh_info.effective_channel is None:
            raise ReleaseUnavailableError(
                kind=ReleaseUnavailableKind.NO_SUITABLE_CHANNEL,
                request=ReleaseRequest(
                    charm_name=charm_name,
                    architecture=ubuntu_arch,
                    platform=platform,
                    base=ubuntu_version,
                    revision=charm_revision,
                ),
                detail=(
                    f"Failed to find suitable channel for charm {charm_name} revision "
                    f"{charm_revision} with Ubuntu {ubuntu_version} and architecture {ubuntu_arch}"
                ),
            )

        return self._ensure_compatibility(
            charm=self._build_charm(
                charm_name=charm_name,
                channel=CharmChannel.model_validate(default_refresh_info.effective_channel),
                revision=charm_revision,
                ubuntu_version=ubuntu_version,
                ubuntu_arch=ubuntu_arch,
                metadata=refresh_info.charm.metadata,
                config_schema=refresh_info.charm.config,
            ),
            juju_version=juju_version,
            platform=platform,
        )

    def _charm_from_store_by_channel(
        self,
        charm_name: str,
        ubuntu_arch: str,
        juju_version: JujuVersion | None,
        platform: str | None,
        charm_channel: CharmChannel,
        ubuntu_version: str | None = None,
    ) -> Charm:
        # Get default ubuntu version if not provided
        if not ubuntu_version:
            ubuntu_version = self._default_ubuntu_version(charm_name, ubuntu_arch, charm_channel=charm_channel)

        # Call refresh with channel and base
        refresh_info = self.http_client.refresh(
            RefreshAction(
                charm_name=charm_name,
                charm_channel=str(charm_channel),
                base=CharmhubBase(
                    channel=ubuntu_version,
                    architecture=ubuntu_arch,
                ),
            )
        )

        # Check for errors and incomplete data
        if refresh_info.error is not None:
            raise ReleaseUnavailableError(
                kind=ReleaseUnavailableKind.CHANNEL_NOT_FOUND,
                request=ReleaseRequest(
                    charm_name=charm_name,
                    architecture=ubuntu_arch,
                    platform=platform,
                    base=ubuntu_version,
                    channel=str(charm_channel),
                ),
                detail=(
                    f"Failed to find release for charm {charm_name} in channel {charm_channel} "
                    f"with Ubuntu {ubuntu_version}: {refresh_info.error.message}"
                ),
                error_code=refresh_info.error.code,
            )

        if refresh_info.charm is None:
            raise IncompleteCharmInfoException(
                f"Refresh info for charm {charm_name} in channel {charm_channel} returned no charm and no error"
            )
        if refresh_info.charm.revision is None:
            raise IncompleteCharmInfoException(
                f"Refresh info for charm {charm_name} in channel {charm_channel} returned no revision"
            )

        return self._ensure_compatibility(
            charm=self._build_charm(
                charm_name=charm_name,
                channel=charm_channel,
                revision=refresh_info.charm.revision,
                ubuntu_version=ubuntu_version,
                ubuntu_arch=ubuntu_arch,
                metadata=refresh_info.charm.metadata,
                config_schema=refresh_info.charm.config,
            ),
            juju_version=juju_version,
            platform=platform,
        )

    def _charm_from_store_by_track_and_revision(
        self,
        charm_name: str,
        ubuntu_arch: str,
        juju_version: JujuVersion | None,
        platform: str | None,
        charm_track: str,
        charm_revision: int,
        ubuntu_version: str | None = None,
    ) -> Charm:
        attempt_errors: list[CharmReleaseNotFoundException] = []
        for risk in ["stable", "candidate", "beta", "edge"]:
            try:
                return self._charm_from_store_by_channel_and_revision(
                    charm_name=charm_name,
                    ubuntu_arch=ubuntu_arch,
                    juju_version=juju_version,
                    platform=platform,
                    charm_channel=CharmChannel(track=charm_track, risk=risk, branch=""),
                    charm_revision=charm_revision,
                    ubuntu_version=ubuntu_version,
                )
            except CharmReleaseNotFoundException as exc:
                attempt_errors.append(exc)
        raise ReleaseUnavailableError(
            kind=ReleaseUnavailableKind.TRACK_NOT_FOUND,
            request=ReleaseRequest(
                charm_name=charm_name,
                architecture=ubuntu_arch,
                platform=platform,
                base=ubuntu_version,
                track=charm_track,
                revision=charm_revision,
            ),
            detail=f"No release found for {charm_name} on track {charm_track!r} in any risk tier",
            causes=tuple(attempt_errors),
        ) from attempt_errors[-1]

    def _charm_from_store_by_track(
        self,
        charm_name: str,
        ubuntu_arch: str,
        juju_version: JujuVersion | None,
        platform: str | None,
        charm_track: str,
        ubuntu_version: str | None = None,
    ) -> Charm:
        attempt_errors: list[CharmReleaseNotFoundException] = []
        for risk in ["stable", "candidate", "beta", "edge"]:
            try:
                return self._charm_from_store_by_channel(
                    charm_name=charm_name,
                    ubuntu_arch=ubuntu_arch,
                    juju_version=juju_version,
                    platform=platform,
                    charm_channel=CharmChannel(track=charm_track, risk=risk, branch=""),
                    ubuntu_version=ubuntu_version,
                )
            except CharmReleaseNotFoundException as exc:
                attempt_errors.append(exc)
        raise ReleaseUnavailableError(
            kind=ReleaseUnavailableKind.TRACK_NOT_FOUND,
            request=ReleaseRequest(
                charm_name=charm_name,
                architecture=ubuntu_arch,
                platform=platform,
                base=ubuntu_version,
                track=charm_track,
            ),
            detail=f"No release found for {charm_name} on track {charm_track!r} in any risk tier",
            causes=tuple(attempt_errors),
        ) from attempt_errors[-1]

    def _charm_from_store_default(
        self,
        charm_name: str,
        ubuntu_arch: str,
        juju_version: JujuVersion | None,
        platform: str | None,
        ubuntu_version: str | None = None,
    ) -> Charm:
        # Get default ubuntu version if not provided
        if not ubuntu_version:
            ubuntu_version = self._default_ubuntu_version(charm_name, ubuntu_arch)

        # Get default refresh info
        refresh_info = self._default_refresh_info(
            charm_name,
            CharmhubBase(
                channel=ubuntu_version,
                architecture=ubuntu_arch,
            ),
        )

        # Check for errors and incomplete data
        if refresh_info.effective_channel is None:
            raise ReleaseUnavailableError(
                kind=ReleaseUnavailableKind.NO_SUITABLE_CHANNEL,
                request=ReleaseRequest(
                    charm_name=charm_name,
                    architecture=ubuntu_arch,
                    platform=platform,
                    base=ubuntu_version,
                ),
                detail=(
                    f"Failed to find suitable channel for charm {charm_name} with Ubuntu "
                    f"{ubuntu_version} and architecture {ubuntu_arch}"
                ),
            )
        if refresh_info.charm is None:
            raise IncompleteCharmInfoException(f"Refresh info for charm {charm_name} returned no charm and no error")
        if refresh_info.charm.revision is None:
            raise IncompleteCharmInfoException(f"Refresh info for charm {charm_name} returned no revision")

        return self._ensure_compatibility(
            charm=self._build_charm(
                charm_name=charm_name,
                channel=CharmChannel.model_validate(refresh_info.effective_channel),
                revision=refresh_info.charm.revision,
                ubuntu_version=ubuntu_version,
                ubuntu_arch=ubuntu_arch,
                metadata=refresh_info.charm.metadata,
                config_schema=refresh_info.charm.config,
            ),
            juju_version=juju_version,
            platform=platform,
        )

    def _get_ubuntu_version_from_bases(
        self,
        bases: list[CharmhubBase],
        ubuntu_arch: str,
        charm_name: str,
        charm_revision: int,
        ubuntu_version: str | None = None,
    ) -> str:
        # Validate provided ubuntu_version is in bases
        if ubuntu_version:
            if CharmhubBase(name="ubuntu", channel=ubuntu_version, architecture=ubuntu_arch) not in bases:
                supported_bases = tuple(
                    base.channel for base in bases if base.name == "ubuntu" and base.architecture == ubuntu_arch
                )
                raise BaseMismatchError(
                    request=ReleaseRequest(
                        charm_name=charm_name,
                        architecture=ubuntu_arch,
                        base=ubuntu_version,
                        revision=charm_revision,
                    ),
                    requested_base=ubuntu_version,
                    supported_bases=supported_bases,
                )
            return ubuntu_version

        # Return first ubuntu version with matching base
        # This matches Juju's behavior when the requested base is empty
        # https://github.com/juju/juju/blob/ed42a9975f6676210e81029b8c0d9c9bd9b152e5/core/charm/computedbase.go#L23
        for base in bases:
            if base.name == "ubuntu" and base.architecture == ubuntu_arch:
                return base.channel

        # No valid ubuntu version found
        raise ArchitectureMismatchError(
            request=ReleaseRequest(
                charm_name=charm_name,
                architecture=ubuntu_arch,
                revision=charm_revision,
            ),
            supported_architectures=tuple(sorted({base.architecture for base in bases if base.name == "ubuntu"})),
        )

    def _get_revision_refresh_info(self, charm_name: str, charm_revision: int) -> RefreshResponse:
        """Get refresh info for a specific revision."""
        refresh_info = self.http_client.refresh(
            RefreshAction(
                charm_name=charm_name,
                charm_revision=charm_revision,
                always_include_base=True,
            )
        )
        if refresh_info.error is not None:
            raise ReleaseUnavailableError(
                kind=ReleaseUnavailableKind.REVISION_NOT_FOUND,
                request=ReleaseRequest(charm_name=charm_name, revision=charm_revision),
                detail=f"Failed to find charm {charm_name} for revision {charm_revision}: {refresh_info.error.message}",
                error_code=refresh_info.error.code,
            )
        return refresh_info

    def _channel_supports_ubuntu_version(
        self, charm_name: str, ubuntu_arch: str, ubuntu_version: str, charm_channel: CharmChannel
    ) -> bool:
        """Return True if the channel has any content for the given ubuntu version and arch."""
        refresh_info = self.http_client.refresh(
            RefreshAction(
                charm_name=charm_name,
                charm_channel=str(charm_channel),
                base=CharmhubBase(
                    channel=ubuntu_version,
                    architecture=ubuntu_arch,
                ),
            )
        )
        return refresh_info.error is None

    def _default_ubuntu_version(
        self, charm_name: str, ubuntu_arch: str, charm_channel: CharmChannel | None = None
    ) -> str:
        # Juju passes "NA" to get the secret "default-bases" error field
        # https://github.com/juju/juju/blob/ed42a9975f6676210e81029b8c0d9c9bd9b152e5/internal/charmhub/refresh.go#L417
        refresh_info = self.http_client.refresh(
            RefreshAction(
                charm_name=charm_name,
                charm_channel=str(charm_channel) if charm_channel is not None else None,
                base=CharmhubBase(
                    name="NA",
                    channel="NA",
                    architecture=ubuntu_arch,
                ),
            )
        )

        # Extract bases from error response
        if refresh_info.error is None:
            raise ReleaseUnavailableError(
                kind=ReleaseUnavailableKind.UNEXPECTED_STORE_RESPONSE,
                request=ReleaseRequest(charm_name=charm_name, architecture=ubuntu_arch),
                detail=f"Failed to find default bases for charm {charm_name}: no error returned",
            )

        if refresh_info.error.code == "invalid-charm-base":
            if refresh_info.error.extra is None:
                raise IncompleteCharmInfoException(f"No extra information for default bases of {charm_name}")
            bases = refresh_info.error.extra.default_bases
        elif refresh_info.error.code == "revision-not-found":
            if refresh_info.error.extra is None:
                raise IncompleteCharmInfoException(f"No extra information for default bases of {charm_name}")
            bases = [release.base for release in refresh_info.error.extra.releases]
        else:
            raise ReleaseUnavailableError(
                kind=ReleaseUnavailableKind.UNEXPECTED_STORE_RESPONSE,
                request=ReleaseRequest(charm_name=charm_name, architecture=ubuntu_arch),
                detail=(
                    f"Failed to find default bases for charm {charm_name}: "
                    f"unexpected error code {refresh_info.error.code}"
                ),
                error_code=refresh_info.error.code,
            )

        # Return the first supported ubuntu version for this arch.
        # This matches Juju's behavior when the requested base is empty.
        # https://github.com/juju/juju/blob/ed42a9975f6676210e81029b8c0d9c9bd9b152e5/core/charm/computedbase.go#L23
        for base in bases:
            if base.name == "ubuntu" and base.architecture == ubuntu_arch:
                return base.channel

        raise ArchitectureMismatchError(
            request=ReleaseRequest(charm_name=charm_name, architecture=ubuntu_arch),
            supported_architectures=tuple(sorted({base.architecture for base in bases if base.name == "ubuntu"})),
        )

    def _default_refresh_info(self, charm_name: str, base: CharmhubBase) -> RefreshResponse:
        # Get refresh info for base
        refresh_info = self.http_client.refresh(RefreshAction(charm_name=charm_name, base=base))
        if refresh_info.error is None:
            return refresh_info

        # If error check extra releases for base
        if refresh_info.error and refresh_info.error.code == "revision-not-found":
            # Gather channels with matching base
            if refresh_info.error.extra is None:
                raise IncompleteCharmInfoException("No error information in refresh response")
            channels = {release.channel for release in refresh_info.error.extra.releases if release.base == base}

            # Try channels with default track as well
            channels |= {f"latest/{channel}" for channel in channels if "/" not in channel}

            # Try calling refresh with each channel
            for channel in channels:
                refresh_info = self.http_client.refresh(
                    RefreshAction(charm_name=charm_name, base=base, charm_channel=channel)
                )
                if refresh_info.error is None:
                    break

        # Check refresh info for error
        if refresh_info.error:
            raise ReleaseUnavailableError(
                kind=ReleaseUnavailableKind.DEFAULT_RELEASE_NOT_FOUND,
                request=ReleaseRequest(
                    charm_name=charm_name,
                    architecture=base.architecture,
                    base=base.channel,
                ),
                detail=(
                    f"Failed to find default release for charm {charm_name} "
                    f"with base {base}: {refresh_info.error.message}"
                ),
                error_code=refresh_info.error.code,
            )

        return refresh_info

    def _get_charm_endpoints(
        self, charm_name: str, metadata: CharmMetadata, channel: CharmChannel
    ) -> dict[str, CharmEndpoint]:
        # Get overrides
        endpoint_overrides = self.overrides_client.get_charm_endpoint_overrides(charm_name, channel)

        # Validate that override keys exist in the charm's metadata.
        for endpoint_type, metadata_map, label in (
            (EndpointType.REQUIRES, metadata.requires, "requires"),
            (EndpointType.PROVIDES, metadata.provides, "provides"),
        ):
            override_map = endpoint_overrides.get(endpoint_type, {})
            stale = sorted(set(override_map) - set(metadata_map))
            if stale:
                raise UnparsableCharmException(
                    f"Charm {charm_name!r} override declares {label} endpoints not present in "
                    f"charm metadata at channel {channel}: {stale}"
                )

        # Gather endpoints
        endpoints = {}
        for endpoint_type, endpoint_map in (
            (EndpointType.PEERS, metadata.peers),
            (EndpointType.REQUIRES, metadata.requires),
            (EndpointType.PROVIDES, metadata.provides),
        ):
            override_map = endpoint_overrides.get(endpoint_type, {})

            for endpoint_name, endpoint in endpoint_map.items():
                endpoint_override = override_map.get(endpoint_name, CharmEndpointOverrides())

                # Calculate optional flag
                if endpoint_override.optional is not None:
                    optional = endpoint_override.optional
                elif endpoint.optional is not None:
                    optional = endpoint.optional
                elif endpoint_type == EndpointType.PEERS:
                    optional = True
                else:
                    optional = False

                # Calculate limit
                limit: int | None
                if endpoint_override.limit is not None:
                    limit = endpoint_override.limit
                else:
                    limit = endpoint.limit

                # Calculate cyclic
                if endpoint_override.cyclic is not None:
                    cyclic = endpoint_override.cyclic
                else:
                    cyclic = False

                # Calculate features
                features = frozenset(endpoint_override.features)

                # Add endpoint
                endpoints[endpoint_name] = CharmEndpoint(
                    type=endpoint_type,
                    interface=endpoint.interface,
                    optional=optional,
                    limit=limit,
                    scope=endpoint.scope,
                    cyclic=cyclic,
                    features=features,
                )

        # Inject implicit juju-info provides endpoint (mirrors Juju's state/application.go Endpoints()).
        # Only non-subordinate (principal) charms implicitly provide juju-info with scope: global.
        # Subordinate charms explicitly require it - they do NOT implicitly provide it.
        # Only inject if the charm does not already explicitly declare it.
        if not metadata.subordinate and JUJU_INFO_INTERFACE not in endpoints:
            endpoints[JUJU_INFO_INTERFACE] = CharmEndpoint(
                type=EndpointType.PROVIDES,
                interface=JUJU_INFO_INTERFACE,
                optional=True,
                limit=None,
                scope=EndpointScope.GLOBAL,
                cyclic=False,
                features=frozenset(),
            )

        return endpoints

    def _get_charm_configs(
        self, charm_name: str, channel: CharmChannel, config_schema: CharmConfigSchema
    ) -> dict[str, list[CharmConfigValue]]:
        config_overrides = self.overrides_client.get_charm_config_overrides(charm_name, channel)
        stale_configs = sorted(set(config_overrides) - set(config_schema.options))
        if stale_configs:
            raise UnparsableCharmException(
                f"Charm {charm_name!r} override declares config keys not present in "
                f"charm config at channel {channel}: {stale_configs}"
            )
        return config_overrides

    def _get_charm_resources(
        self, charm_name: str, channel: CharmChannel, metadata: CharmMetadata
    ) -> dict[str, list[CharmResourceValue]]:
        resource_overrides = self.overrides_client.get_charm_resource_overrides(charm_name, channel)
        stale_resources = sorted(set(resource_overrides) - set(metadata.resources))
        if stale_resources:
            raise UnparsableCharmException(
                f"Charm {charm_name!r} override declares resource keys not present in "
                f"charm metadata at channel {channel}: {stale_resources}"
            )
        return resource_overrides

    def _get_charm_constraints(self, charm_name: str, channel: CharmChannel) -> list[AnyExpr]:
        """Parse raw DSL constraint strings from overrides into typed AST nodes."""
        result: list[AnyExpr] = []
        for text in self.overrides_client.get_charm_constraints_overrides(charm_name, channel):
            expr = parse_constraint(text)
            if expr.dsl_type not in (DSLType.BOOL, DSLType.RUNTIME):
                raise ValueError(
                    f"Constraint for charm {charm_name!r} must be a boolean expression, "
                    f"got {expr.dsl_type.value}: {text!r}"
                )
            result.append(expr)
        return result

    def _get_charm_assumes(self, charm_name: str, metadata: CharmMetadata, channel: CharmChannel) -> CharmAssumesEntry:
        # Get overrides
        assumes_overrides = self.overrides_client.get_charm_assumes_overrides(charm_name, channel)
        if assumes_overrides is not None:
            assumes = assumes_overrides
        else:
            assumes = metadata.assumes

        # Return parsed assumes entry
        return CharmAssumesEntry(all_of=frozenset(self._get_assumes_entry(e) for e in assumes))

    def _get_charm_platforms(self, charm_name: str, channel: CharmChannel, metadata: CharmMetadata) -> list[str]:
        """Return the platform(s) this charm may be deployed to.

        Platform overrides win when present. Otherwise, fall back to the charm's own
        metadata (mirrors ``_get_charm_assumes``). Three independent metadata.yaml
        conventions identify a Kubernetes (sidecar) charm: a non-empty ``containers``
        block (current Charmcraft charms), a legacy ``series: [kubernetes]`` entry
        (pre-Charmcraft "reactive"/podspec charms, which predate ``containers``), or an
        unconditionally required ``k8s-api`` feature in ``assumes`` (charms that only
        talk to the Kubernetes API and host no workload container of their own, e.g.
        nginx-ingress-integrator - see _PLATFORM_FEATURES, which defines k8s-api as
        kubernetes-only). The absence of all three identifies a machine charm.
        """
        platform_overrides = self.overrides_client.get_charm_platform_overrides(charm_name)
        if platform_overrides is not None:
            return platform_overrides or ["machine"]
        assumes = self._get_charm_assumes(charm_name, metadata, channel)
        is_kubernetes = (
            bool(metadata.containers)
            or "kubernetes" in metadata.series
            or self._assumes_entry_requires_feature(assumes, "k8s-api")
        )
        return ["kubernetes"] if is_kubernetes else ["machine"]

    def _assumes_entry_requires_feature(self, entry: CharmAssumesEntry, feature: str) -> bool:
        """Whether an ``assumes`` entry unconditionally requires the given feature."""
        if entry.feature == feature:
            return True
        if entry.all_of:
            return any(self._assumes_entry_requires_feature(e, feature) for e in entry.all_of)
        if entry.any_of:
            return all(self._assumes_entry_requires_feature(e, feature) for e in entry.any_of)
        return False

    def _get_assumes_entry(self, raw: str | dict[str, Any]) -> CharmAssumesEntry:
        """Translate a raw charmhub assumes entry (wire format) into a domain CharmAssumesEntry."""
        if isinstance(raw, str):
            match = _ASSUMES_JUJU_RE.match(raw)
            if match:
                op_str, version_str = match.group(1), match.group(2)
                try:
                    return CharmAssumesEntry(op=op_str, required_version=JujuVersion.parse(version_str))
                except ValueError:
                    self.logger.warning(
                        f"Could not parse Juju version constraint {raw!r}: version string {version_str!r} "
                        "is not a valid Juju version. Treating as unsatisfied feature."
                    )
            return CharmAssumesEntry(feature=raw)

        if isinstance(raw, dict):
            if "any-of" in raw:
                return CharmAssumesEntry(any_of=frozenset(self._get_assumes_entry(sub) for sub in raw["any-of"]))
            if "all-of" in raw:
                return CharmAssumesEntry(all_of=frozenset(self._get_assumes_entry(sub) for sub in raw["all-of"]))

        return CharmAssumesEntry(feature=str(raw))
