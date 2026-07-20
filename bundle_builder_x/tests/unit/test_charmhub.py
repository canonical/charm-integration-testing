# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import cast

import pytest
from pydantic.dataclasses import dataclass

from bundle_builder_x.charm import CharmChannel, EndpointScope, EndpointType
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.charmhub_http import (
    CharmConfigSchema,
    CharmhubBase,
    CharmhubHttpClient,
    CharmMetadata,
    CharmReleaseNotFoundException,
    FindResponse,
    IncompleteCharmInfoException,
    InfoResponse,
    RefreshAction,
    RefreshResponse,
    UnparsableCharmException,
)
from bundle_builder_x.overrides import CharmGlobalOverrides, OverridesClient

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _NullHttpClient:
    """Base stub that raises NotImplementedError for all HTTP methods."""

    def refresh(self, action: RefreshAction) -> RefreshResponse:
        raise NotImplementedError

    def find(self, provides: str | None = None, requires: str | None = None) -> list[FindResponse]:
        raise NotImplementedError

    def info(self, charm: str) -> InfoResponse:
        raise NotImplementedError


def _make_client(http: _NullHttpClient) -> CharmhubClient:
    """Wrap a stub in a CharmhubClient without triggering mypy override errors."""
    return CharmhubClient(http_client=cast(CharmhubHttpClient, http))


class _StubOverridesClient(OverridesClient):
    """OverridesClient that returns a fixed CharmGlobalOverrides for any charm."""

    def __init__(self, raw: dict[str, object]) -> None:
        super().__init__()
        self._overrides = CharmGlobalOverrides(**raw)

    def _get_charm_global_overrides(self, charm: str) -> CharmGlobalOverrides:  # type: ignore[override]
        return self._overrides


def _client(raw: dict[str, object]) -> CharmhubClient:
    return CharmhubClient(overrides_client=_StubOverridesClient(raw))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(name: str = "ceph-mon") -> RefreshResponse:
    return RefreshResponse(name=name)


def _error_response(
    name: str,
    code: str,
    message: str,
    extra: RefreshResponse.Error.Extra | None = None,
) -> RefreshResponse:
    return RefreshResponse(
        name=name,
        error=RefreshResponse.Error(code=code, message=message, extra=extra),
    )


def _info_response_with_channels(*track_risk_pairs: tuple[str, str]) -> InfoResponse:
    """Build an InfoResponse whose channel_map contains the given (track, risk) pairs."""
    entries = [
        InfoResponse.ChannelMapEntry(channel=InfoResponse.ChannelMapEntry.Channel(name=f"{t}/{r}", track=t, risk=r))
        for t, r in track_risk_pairs
    ]
    return InfoResponse(channel_map=entries)


class _StubHttpClient(CharmhubHttpClient):
    def __init__(self, info_response: InfoResponse) -> None:
        self._response = info_response

    def info(self, charm: str, include_channel_map: bool = False) -> InfoResponse:  # type: ignore[override]
        return self._response


_CHANNEL = CharmChannel(track="latest", risk="stable", branch="")
_METADATA_REQUIRES = CharmMetadata(requires={"db": CharmMetadata.Endpoint(interface="pgsql")})
_METADATA_PROVIDES = CharmMetadata(provides={"web": CharmMetadata.Endpoint(interface="http")})
_METADATA_WITH_CONTAINERS = CharmMetadata(containers={"app": {"resource": "app-image"}})
_EMPTY_CONFIG = CharmConfigSchema()


def _refresh_response_with_charm(name: str, revision: int, metadata: CharmMetadata) -> RefreshResponse:
    """Build a successful RefreshResponse carrying charm metadata.

    Bypasses the metadata-yaml/config-yaml string parsing (used to decode the real
    Charmhub API response) via model_construct, since the fields here are already the
    typed values that parsing would produce.
    """
    charm = RefreshResponse.Charm.model_construct(revision=revision, metadata=metadata, config=_EMPTY_CONFIG)
    return RefreshResponse.model_construct(name=name, charm=charm, effective_channel=None, error=None)


class TestCharmhubClient:
    # ---------------------------------------------------------------------------
    # TestBuildCharmPlatforms
    # ---------------------------------------------------------------------------

    class TestBuildCharmPlatforms:
        """Charm.platforms is sourced from overrides at build time when present, and
        otherwise falls back to the charm's own metadata (mirrors _get_charm_assumes),
        rather than each call site re-querying the overrides client.
        """

        def test_build_charm_populates_platforms_from_overrides(self) -> None:
            # GIVEN an overrides client that restricts a charm to specific platforms
            client = _client({"platforms": ["kubernetes"]})

            # WHEN building a Charm from store metadata
            charm = client._build_charm(
                charm_name="ceph-mon",
                channel=_CHANNEL,
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                metadata=_METADATA_REQUIRES,
                config_schema=_EMPTY_CONFIG,
            )

            # THEN the charm carries the overridden platforms, not the metadata-derived ones
            assert charm.platforms == ["kubernetes"]

        def test_build_charm_falls_back_to_machine_without_overrides_or_containers(self) -> None:
            # GIVEN an overrides client with no platform override for the charm, and metadata
            # with no `containers` block (a machine charm)
            client = _client({})

            # WHEN building a Charm from store metadata
            charm = client._build_charm(
                charm_name="ceph-mon",
                channel=_CHANNEL,
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                metadata=_METADATA_REQUIRES,
                config_schema=_EMPTY_CONFIG,
            )

            # THEN the charm falls back to the machine platform
            assert charm.platforms == ["machine"]

        def test_build_charm_falls_back_to_kubernetes_when_metadata_has_containers(self) -> None:
            # GIVEN an overrides client with no platform override for the charm, and metadata
            # with a `containers` block (a Kubernetes sidecar charm)
            client = _client({})

            # WHEN building a Charm from store metadata
            charm = client._build_charm(
                charm_name="ceph-mon",
                channel=_CHANNEL,
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                metadata=_METADATA_WITH_CONTAINERS,
                config_schema=_EMPTY_CONFIG,
            )

            # THEN the charm falls back to the kubernetes platform
            assert charm.platforms == ["kubernetes"]

    # ---------------------------------------------------------------------------
    # TestEnsureCompatibility
    # ---------------------------------------------------------------------------

    class TestEnsureCompatibility:
        """CharmhubClient._ensure_compatibility checks the requested platform against
        charm.platforms directly - it does not re-query the overrides client.
        """

        def test_raises_when_platform_not_in_charm_platforms(self) -> None:
            # GIVEN a built charm restricted to the kubernetes platform
            client = _client({})
            charm = client._build_charm(
                charm_name="ceph-mon",
                channel=_CHANNEL,
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                metadata=_METADATA_REQUIRES,
                config_schema=_EMPTY_CONFIG,
            ).model_copy(update={"platforms": ["kubernetes"]})

            # WHEN checking compatibility against the machine platform
            # THEN it is rejected
            with pytest.raises(CharmReleaseNotFoundException):
                client._ensure_compatibility(charm, juju_version=None, platform="machine")

        def test_passes_when_platform_in_charm_platforms(self) -> None:
            # GIVEN a built charm that supports the machine platform
            client = _client({})
            charm = client._build_charm(
                charm_name="ceph-mon",
                channel=_CHANNEL,
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                metadata=_METADATA_REQUIRES,
                config_schema=_EMPTY_CONFIG,
            ).model_copy(update={"platforms": ["machine"]})

            # WHEN checking compatibility against the machine platform
            result = client._ensure_compatibility(charm, juju_version=None, platform="machine")

            # THEN the same charm is returned unchanged
            assert result is charm

        def test_passes_when_charm_platforms_is_none(self) -> None:
            # GIVEN a built charm with no platform restriction, e.g. a hand-constructed test
            # double that never went through _get_charm_platforms
            client = _client({})
            charm = client._build_charm(
                charm_name="ceph-mon",
                channel=_CHANNEL,
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                metadata=_METADATA_REQUIRES,
                config_schema=_EMPTY_CONFIG,
            ).model_copy(update={"platforms": None})
            assert charm.platforms is None

            # WHEN checking compatibility against any platform
            result = client._ensure_compatibility(charm, juju_version=None, platform="kubernetes")

            # THEN it is accepted
            assert result is charm

    # ---------------------------------------------------------------------------
    # TestCharmFromStorePlatformOverrides
    # ---------------------------------------------------------------------------

    class TestCharmFromStorePlatformOverrides:
        """Platform-override enforcement in _ensure_compatibility() is exercised through
        charm_from_store() end-to-end, not just by calling _ensure_compatibility directly
        with a hand-built Charm (see TestEnsureCompatibility above).
        """

        def _client_and_stub(self, raw_overrides: dict[str, object]) -> CharmhubClient:
            response = _refresh_response_with_charm("ceph-mon", revision=5, metadata=_METADATA_REQUIRES)

            class _StubClient(_NullHttpClient):
                def refresh(self, action: RefreshAction) -> RefreshResponse:
                    return response

            return CharmhubClient(
                http_client=cast(CharmhubHttpClient, _StubClient()),
                overrides_client=_StubOverridesClient(raw_overrides),
            )

        def test_raises_when_overrides_disallow_the_requested_platform(self) -> None:
            # GIVEN an overrides client restricting the charm to kubernetes only
            client = self._client_and_stub({"platforms": ["kubernetes"]})

            # WHEN fetching the charm for the machine platform, which the overrides disallow
            # THEN CharmReleaseNotFoundException is raised
            with pytest.raises(CharmReleaseNotFoundException):
                client.charm_from_store(
                    charm_name="ceph-mon",
                    ubuntu_arch="amd64",
                    ubuntu_version="22.04",
                    charm_track="latest",
                    charm_risk="stable",
                    platform="machine",
                )

        def test_passes_when_overrides_allow_the_requested_platform(self) -> None:
            # GIVEN an overrides client that allows the machine platform
            client = self._client_and_stub({"platforms": ["machine"]})

            # WHEN fetching the charm for the machine platform
            charm = client.charm_from_store(
                charm_name="ceph-mon",
                ubuntu_arch="amd64",
                ubuntu_version="22.04",
                charm_track="latest",
                charm_risk="stable",
                platform="machine",
            )

            # THEN it succeeds and carries the overridden platform
            assert charm.platforms == ["machine"]

        def test_passes_when_no_platform_overrides_exist(self) -> None:
            # GIVEN an overrides client with no platform override, and metadata for a
            # machine charm (no `containers` block)
            client = self._client_and_stub({})

            # WHEN fetching the charm for the machine platform, which the metadata-derived
            # platform satisfies
            charm = client.charm_from_store(
                charm_name="ceph-mon",
                ubuntu_arch="amd64",
                ubuntu_version="22.04",
                charm_track="latest",
                charm_risk="stable",
                platform="machine",
            )

            # THEN it succeeds
            assert charm.platforms == ["machine"]

    # ---------------------------------------------------------------------------
    # TestChannelSupportsUbuntuVersion
    # ---------------------------------------------------------------------------

    class TestChannelSupportsUbuntuVersion:
        @dataclass
        class Params:
            label: str
            response: RefreshResponse
            expected: bool

        test_cases = [
            Params(
                label="returns_true_when_no_error",
                response=_ok_response(),
                expected=True,
            ),
            Params(
                label="returns_false_on_revision_not_found",
                response=_error_response("ceph-mon", "revision-not-found", "revision not found"),
                expected=False,
            ),
            Params(
                label="returns_false_on_invalid_charm_base",
                response=_error_response("ceph-mon", "invalid-charm-base", "invalid base"),
                expected=False,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
        def test_return_value(self, params: Params) -> None:
            # GIVEN a stub http client returning the configured response
            class _StubClient(_NullHttpClient):
                def refresh(self, action: RefreshAction) -> RefreshResponse:
                    return params.response

            client = _make_client(_StubClient())

            # WHEN checking if the channel supports an ubuntu version
            result = client._channel_supports_ubuntu_version(
                charm_name="ceph-mon",
                ubuntu_arch="amd64",
                ubuntu_version="24.04",
                charm_channel=CharmChannel(track="latest", risk="stable", branch=""),
            )

            # THEN the result matches expected
            assert result == params.expected

        def test_passes_correct_action(self) -> None:
            # GIVEN a stub http client that captures the refresh action
            captured: list[RefreshAction] = []

            class _StubClient(_NullHttpClient):
                def refresh(self, action: RefreshAction) -> RefreshResponse:
                    captured.append(action)
                    return _ok_response()

            client = _make_client(_StubClient())
            channel = CharmChannel(track="tentacle", risk="edge", branch="")

            # WHEN calling _channel_supports_ubuntu_version
            client._channel_supports_ubuntu_version(
                charm_name="ceph-mon",
                ubuntu_arch="amd64",
                ubuntu_version="26.04",
                charm_channel=channel,
            )

            # THEN refresh is called with the channel string and the requested base
            assert len(captured) == 1
            assert captured[0] == RefreshAction(
                charm_name="ceph-mon",
                charm_channel="tentacle/edge",
                base=CharmhubBase(channel="26.04", architecture="amd64"),
            )

    # ---------------------------------------------------------------------------
    # TestGetRevisionRefreshInfo
    # ---------------------------------------------------------------------------

    class TestGetRevisionRefreshInfo:
        def test_returns_response_when_no_error(self) -> None:
            # GIVEN a stub that returns a valid response
            expected = _ok_response()

            class _StubClient(_NullHttpClient):
                def refresh(self, action: RefreshAction) -> RefreshResponse:
                    return expected

            client = _make_client(_StubClient())

            # WHEN getting refresh info for a revision
            result = client._get_revision_refresh_info("ceph-mon", 519)

            # THEN the response is returned unchanged
            assert result == expected

        def test_raises_on_error(self) -> None:
            # GIVEN a stub that returns an error response
            class _StubClient(_NullHttpClient):
                def refresh(self, action: RefreshAction) -> RefreshResponse:
                    return _error_response("ceph-mon", "revision-not-found", "revision 519 not found")

            client = _make_client(_StubClient())

            # WHEN getting refresh info for a revision that does not exist
            # THEN CharmReleaseNotFoundException is raised
            with pytest.raises(CharmReleaseNotFoundException, match="519"):
                client._get_revision_refresh_info("ceph-mon", 519)

        def test_passes_correct_action(self) -> None:
            # GIVEN a stub that captures the refresh action
            captured: list[RefreshAction] = []

            class _StubClient(_NullHttpClient):
                def refresh(self, action: RefreshAction) -> RefreshResponse:
                    captured.append(action)
                    return _ok_response()

            client = _make_client(_StubClient())

            # WHEN getting refresh info for a revision
            client._get_revision_refresh_info("ceph-mon", 519)

            # THEN refresh is called with just the revision and always_include_base=True,
            # and no channel or base filter
            assert len(captured) == 1
            assert captured[0] == RefreshAction(
                charm_name="ceph-mon",
                charm_revision=519,
                always_include_base=True,
            )

    # ---------------------------------------------------------------------------
    # TestGetUbuntuVersionFromBases
    # ---------------------------------------------------------------------------

    class TestGetUbuntuVersionFromBases:
        @dataclass
        class Params:
            label: str
            bases: list[CharmhubBase]
            ubuntu_arch: str
            ubuntu_version: str | None
            expected_version: str | None
            raises: type[Exception] | None = None

        test_cases = [
            Params(
                label="provided_version_matches_base",
                bases=[CharmhubBase(channel="24.04", architecture="amd64")],
                ubuntu_arch="amd64",
                ubuntu_version="24.04",
                expected_version="24.04",
            ),
            Params(
                label="provided_version_not_in_bases_raises",
                bases=[CharmhubBase(channel="24.04", architecture="amd64")],
                ubuntu_arch="amd64",
                ubuntu_version="26.04",
                expected_version=None,
                raises=CharmReleaseNotFoundException,
            ),
            Params(
                label="provided_version_wrong_arch_raises",
                bases=[CharmhubBase(channel="24.04", architecture="arm64")],
                ubuntu_arch="amd64",
                ubuntu_version="24.04",
                expected_version=None,
                raises=CharmReleaseNotFoundException,
            ),
            Params(
                label="no_version_returns_first_matching_arch",
                bases=[
                    CharmhubBase(channel="22.04", architecture="arm64"),
                    CharmhubBase(channel="24.04", architecture="amd64"),
                    CharmhubBase(channel="26.04", architecture="amd64"),
                ],
                ubuntu_arch="amd64",
                ubuntu_version=None,
                expected_version="24.04",
            ),
            Params(
                label="no_version_no_matching_arch_raises",
                bases=[CharmhubBase(channel="24.04", architecture="arm64")],
                ubuntu_arch="amd64",
                ubuntu_version=None,
                expected_version=None,
                raises=CharmReleaseNotFoundException,
            ),
            Params(
                label="no_version_empty_bases_raises",
                bases=[],
                ubuntu_arch="amd64",
                ubuntu_version=None,
                expected_version=None,
                raises=CharmReleaseNotFoundException,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
        def test(self, params: Params) -> None:
            client = _make_client(_NullHttpClient())

            # WHEN calling _get_ubuntu_version_from_bases
            if params.raises is not None:
                # THEN the expected exception is raised
                with pytest.raises(params.raises):
                    client._get_ubuntu_version_from_bases(
                        params.bases, params.ubuntu_arch, "ceph-mon", 519, params.ubuntu_version
                    )
            else:
                result = client._get_ubuntu_version_from_bases(
                    params.bases, params.ubuntu_arch, "ceph-mon", 519, params.ubuntu_version
                )
                assert result == params.expected_version

    # ---------------------------------------------------------------------------
    # TestDefaultUbuntuVersion
    # ---------------------------------------------------------------------------

    class TestDefaultUbuntuVersion:
        @dataclass
        class Params:
            label: str
            response: RefreshResponse
            ubuntu_arch: str
            expected_version: str | None
            raises: type[Exception] | None = None

        test_cases = [
            Params(
                label="invalid_charm_base_returns_first_matching_version",
                response=_error_response(
                    "ceph-mon",
                    "invalid-charm-base",
                    "invalid base",
                    extra=RefreshResponse.Error.Extra(
                        default_bases=[
                            CharmhubBase(channel="22.04", architecture="arm64"),
                            CharmhubBase(channel="24.04", architecture="amd64"),
                            CharmhubBase(channel="26.04", architecture="amd64"),
                        ]
                    ),
                ),
                ubuntu_arch="amd64",
                expected_version="24.04",
            ),
            Params(
                label="revision_not_found_returns_first_matching_version_from_releases",
                response=_error_response(
                    "ceph-mon",
                    "revision-not-found",
                    "not found",
                    extra=RefreshResponse.Error.Extra(
                        releases=[
                            RefreshResponse.Error.Extra.Release(
                                base=CharmhubBase(channel="22.04", architecture="arm64"),
                                channel="latest/stable",
                            ),
                            RefreshResponse.Error.Extra.Release(
                                base=CharmhubBase(channel="24.04", architecture="amd64"),
                                channel="latest/stable",
                            ),
                        ]
                    ),
                ),
                ubuntu_arch="amd64",
                expected_version="24.04",
            ),
            Params(
                label="no_error_raises",
                response=_ok_response(),
                ubuntu_arch="amd64",
                expected_version=None,
                raises=CharmReleaseNotFoundException,
            ),
            Params(
                label="unexpected_error_code_raises",
                response=_error_response("ceph-mon", "some-other-error", "unexpected"),
                ubuntu_arch="amd64",
                expected_version=None,
                raises=CharmReleaseNotFoundException,
            ),
            Params(
                label="invalid_charm_base_no_matching_arch_raises",
                response=_error_response(
                    "ceph-mon",
                    "invalid-charm-base",
                    "invalid base",
                    extra=RefreshResponse.Error.Extra(
                        default_bases=[CharmhubBase(channel="24.04", architecture="arm64")]
                    ),
                ),
                ubuntu_arch="amd64",
                expected_version=None,
                raises=CharmReleaseNotFoundException,
            ),
            Params(
                label="invalid_charm_base_no_extra_raises",
                response=_error_response("ceph-mon", "invalid-charm-base", "invalid base", extra=None),
                ubuntu_arch="amd64",
                expected_version=None,
                raises=IncompleteCharmInfoException,
            ),
            Params(
                label="revision_not_found_no_extra_raises",
                response=_error_response("ceph-mon", "revision-not-found", "not found", extra=None),
                ubuntu_arch="amd64",
                expected_version=None,
                raises=IncompleteCharmInfoException,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN a stub http client returning the configured response
            class _StubClient(_NullHttpClient):
                def refresh(self, action: RefreshAction) -> RefreshResponse:
                    return params.response

            client = _make_client(_StubClient())

            # WHEN calling _default_ubuntu_version
            if params.raises is not None:
                # THEN the expected exception is raised
                with pytest.raises(params.raises):
                    client._default_ubuntu_version("ceph-mon", params.ubuntu_arch)
            else:
                result = client._default_ubuntu_version("ceph-mon", params.ubuntu_arch)
                assert result == params.expected_version

    # ---------------------------------------------------------------------------
    # TestGetCharmChannels
    # ---------------------------------------------------------------------------

    class TestGetCharmChannels:
        def test_risk_order_within_track(self) -> None:
            # GIVEN a charm published on latest/edge, latest/beta, latest/candidate, latest/stable
            class _StubClient(_NullHttpClient):
                def info(self, charm: str, *, include_channel_map: bool = False) -> InfoResponse:
                    return _info_response_with_channels(
                        ("latest", "edge"), ("latest", "beta"), ("latest", "candidate"), ("latest", "stable")
                    )

            client = _make_client(_StubClient())
            # WHEN fetching channels
            channels = client.get_charm_channels("mycharm")
            # THEN they are ordered stable first within the track
            assert [c.risk for c in channels] == ["stable", "candidate", "beta", "edge"]

        def test_track_order_is_alphabetical(self) -> None:
            # GIVEN a charm published on tracks "1.0" and "2.0" (both stable)
            class _StubClient(_NullHttpClient):
                def info(self, charm: str, *, include_channel_map: bool = False) -> InfoResponse:
                    return _info_response_with_channels(("2.0", "stable"), ("1.0", "stable"))

            client = _make_client(_StubClient())
            # WHEN fetching channels
            channels = client.get_charm_channels("mycharm")
            # THEN tracks are sorted alphabetically
            assert [c.track for c in channels] == ["1.0", "2.0"]

        def test_deduplicates_channels(self) -> None:
            # GIVEN a channel-map with duplicate entries for the same channel
            class _StubClient(_NullHttpClient):
                def info(self, charm: str, *, include_channel_map: bool = False) -> InfoResponse:
                    return _info_response_with_channels(("latest", "stable"), ("latest", "stable"))

            client = _make_client(_StubClient())
            # WHEN fetching channels
            channels = client.get_charm_channels("mycharm")
            # THEN duplicates are removed
            assert len(channels) == 1

    # ---------------------------------------------------------------------------
    # TestGetCharmEndpoints
    # ---------------------------------------------------------------------------

    class TestGetCharmEndpoints:
        def test_stale_requires_key_raises(self) -> None:
            # GIVEN an override declaring a requires endpoint absent from charm metadata
            client = _client({"overrides": [{"requires": {"logging": {"optional": True}}}]})
            # WHEN building endpoints
            # THEN UnparsableCharmException is raised mentioning "requires"
            with pytest.raises(UnparsableCharmException, match="requires"):
                client._get_charm_endpoints("mycharm", _METADATA_REQUIRES, _CHANNEL)

        def test_valid_requires_key_passes(self) -> None:
            # GIVEN an override for a requires endpoint that exists in metadata
            client = _client({"overrides": [{"requires": {"db": {"optional": True}}}]})
            # WHEN building endpoints
            endpoints = client._get_charm_endpoints("mycharm", _METADATA_REQUIRES, _CHANNEL)
            # THEN the endpoint is present
            assert "db" in endpoints

        def test_stale_provides_key_raises(self) -> None:
            # GIVEN an override declaring a provides endpoint absent from charm metadata
            client = _client({"overrides": [{"provides": {"grafana-dashboard": {}}}]})
            # WHEN building endpoints
            # THEN UnparsableCharmException is raised mentioning "provides"
            with pytest.raises(UnparsableCharmException, match="provides"):
                client._get_charm_endpoints("mycharm", _METADATA_PROVIDES, _CHANNEL)

        def test_valid_provides_key_passes(self) -> None:
            # GIVEN an override for a provides endpoint that exists in metadata
            client = _client({"overrides": [{"provides": {"web": {"optional": True}}}]})
            # WHEN building endpoints
            endpoints = client._get_charm_endpoints("mycharm", _METADATA_PROVIDES, _CHANNEL)
            # THEN the endpoint is present
            assert "web" in endpoints

        def test_no_override_returns_metadata_endpoints(self) -> None:
            # GIVEN no overrides
            client = _client({})
            # WHEN building endpoints
            endpoints = client._get_charm_endpoints("mycharm", _METADATA_REQUIRES, _CHANNEL)
            # THEN all metadata endpoints are returned
            assert "db" in endpoints

        def test_injects_juju_info_when_absent(self) -> None:
            client = _client({})
            endpoints = client._get_charm_endpoints("myapp", _METADATA_REQUIRES, _CHANNEL)
            assert "juju-info" in endpoints
            ep = endpoints["juju-info"]
            assert ep.type == EndpointType.PROVIDES
            assert ep.interface == "juju-info"
            assert ep.optional is True
            assert ep.scope == EndpointScope.GLOBAL

        def test_subordinate_does_not_get_implicit_juju_info(self) -> None:
            # Subordinate charms explicitly REQUIRE juju-info; they do not implicitly provide it.
            # Only non-subordinate (principal) charms get the implicit provides injection.
            metadata = CharmMetadata(
                subordinate=True,
                requires={"general-info": CharmMetadata.Endpoint(interface="juju-info", scope="container")},
            )
            client = _client({})
            endpoints = client._get_charm_endpoints("nrpe", metadata, _CHANNEL)
            assert "juju-info" not in endpoints

        def test_does_not_overwrite_explicit_juju_info(self) -> None:
            metadata = CharmMetadata(provides={"juju-info": CharmMetadata.Endpoint(interface="juju-info", limit=5)})
            client = _client({})
            endpoints = client._get_charm_endpoints("special", metadata, _CHANNEL)
            # Explicit declaration wins; the injected one would have limit=None
            assert endpoints["juju-info"].limit == 5

        def test_preserves_container_scope_from_metadata(self) -> None:
            metadata = CharmMetadata(
                requires={"general-info": CharmMetadata.Endpoint(interface="juju-info", scope="container")}
            )
            client = _client({})
            endpoints = client._get_charm_endpoints("nrpe", metadata, _CHANNEL)
            assert endpoints["general-info"].scope == EndpointScope.CONTAINER

        def test_scope_none_when_metadata_has_no_scope(self) -> None:
            client = _client({})
            endpoints = client._get_charm_endpoints("myapp", _METADATA_REQUIRES, _CHANNEL)
            assert endpoints["db"].scope is None

    class TestCharmMetadataSubordinateFields:
        """CharmMetadata.subordinate and CharmMetadata.Endpoint.scope fields."""

        def test_subordinate_defaults_false(self) -> None:
            assert CharmMetadata().subordinate is False

        def test_subordinate_set_true(self) -> None:
            assert CharmMetadata(subordinate=True).subordinate is True

        def test_endpoint_scope_defaults_none(self) -> None:
            assert CharmMetadata.Endpoint(interface="juju-info").scope is None

        def test_endpoint_scope_stored(self) -> None:
            ep = CharmMetadata.Endpoint(interface="juju-info", scope="container")
            assert ep.scope == EndpointScope.CONTAINER

    # ---------------------------------------------------------------------------
    # TestGetCharmConfigs
    # ---------------------------------------------------------------------------

    class TestGetCharmConfigs:
        def test_stale_config_key_raises(self) -> None:
            # GIVEN an override declaring a config key absent from the charm's config schema
            client = _client({"overrides": [{"configs": {"gone-option": ["value"]}}]})
            # WHEN building configs
            # THEN UnparsableCharmException is raised mentioning "config"
            with pytest.raises(UnparsableCharmException, match="config"):
                client._get_charm_configs("mycharm", _CHANNEL, _EMPTY_CONFIG)

        def test_valid_config_key_passes(self) -> None:
            # GIVEN an override for a config key that exists in the schema
            schema = CharmConfigSchema(options={"my-option": CharmConfigSchema.Option(type="string")})
            client = _client({"overrides": [{"configs": {"my-option": ["v1"]}}]})
            # WHEN building configs
            result = client._get_charm_configs("mycharm", _CHANNEL, schema)
            # THEN the override value is returned
            assert result == {"my-option": ["v1"]}

        def test_no_config_override_returns_empty(self) -> None:
            # GIVEN no config overrides
            client = _client({})
            # WHEN building configs
            # THEN an empty dict is returned
            assert client._get_charm_configs("mycharm", _CHANNEL, _EMPTY_CONFIG) == {}

    class TestGetCharmResources:
        def test_stale_resource_key_raises(self) -> None:
            # GIVEN an override declaring a resource key absent from the charm's metadata
            client = _client({"overrides": [{"resources": {"gone-image": ["ghcr.io/foo:latest"]}}]})
            # WHEN building resources
            # THEN UnparsableCharmException is raised mentioning "resource"
            with pytest.raises(UnparsableCharmException, match="resource"):
                client._get_charm_resources("mycharm", _CHANNEL, CharmMetadata())

        def test_valid_resource_key_passes(self) -> None:
            # GIVEN an override for a resource key that exists in the metadata
            metadata = CharmMetadata(resources={"my-image": CharmMetadata.Resource(type="oci-image")})
            client = _client({"overrides": [{"resources": {"my-image": ["ghcr.io/foo:v1"]}}]})
            # WHEN building resources
            result = client._get_charm_resources("mycharm", _CHANNEL, metadata)
            # THEN the override value is returned
            assert result == {"my-image": ["ghcr.io/foo:v1"]}

        def test_no_resource_override_returns_empty(self) -> None:
            # GIVEN no resource overrides
            client = _client({})
            # WHEN building resources
            # THEN an empty dict is returned
            assert client._get_charm_resources("mycharm", _CHANNEL, CharmMetadata()) == {}

    # ---------------------------------------------------------------------------
    # TestFindCharms
    # ---------------------------------------------------------------------------

    class TestFindCharms:
        class _MultiOverridesClient(OverridesClient):
            """OverridesClient with per-charm listed overrides."""

            def __init__(self, listed_by_charm: dict[str, bool | None]) -> None:
                super().__init__()
                self._listed_by_charm = listed_by_charm

            def _get_all_charm_global_overrides(self) -> dict[str, CharmGlobalOverrides]:  # type: ignore[override]
                return {charm: CharmGlobalOverrides(listed=val) for charm, val in self._listed_by_charm.items()}

        def _find_client(
            self, find_names: list[str], listed_by_charm: dict[str, bool | None] | None = None
        ) -> CharmhubClient:
            class _StubFindClient(_NullHttpClient):
                def find(self, provides: str | None = None, requires: str | None = None) -> list[FindResponse]:
                    return [
                        FindResponse(name=name, result=FindResponse.Result(**{"deployable-on": {"machine"}}))
                        for name in find_names
                    ]

                def info(self, charm: str, include_channel_map: bool = False) -> InfoResponse:
                    return InfoResponse(result=InfoResponse.Result(**{"deployable-on": frozenset({"machine"})}))

            overrides = self._MultiOverridesClient(listed_by_charm or {})
            return CharmhubClient(
                http_client=cast(CharmhubHttpClient, _StubFindClient()),
                overrides_client=overrides,
            )

        def test_delisted_charm_excluded_from_api_results(self) -> None:
            # GIVEN the API returns two charms and one has listed: false
            client = self._find_client(
                find_names=["charm-a", "charm-b"],
                listed_by_charm={"charm-b": False},
            )

            # WHEN finding charms
            result = client.find_charms()

            # THEN the delisted charm is excluded
            assert "charm-a" in result
            assert "charm-b" not in result

        def test_delisted_charm_not_added_when_absent_from_api(self) -> None:
            # GIVEN the API returns one charm and a different charm has listed: false
            client = self._find_client(
                find_names=["charm-a"],
                listed_by_charm={"charm-c": False},
            )

            # WHEN finding charms
            result = client.find_charms()

            # THEN charm-a is returned and charm-c is not present
            assert result == {"charm-a"}

        def test_no_delisting_override_returns_all_api_results(self) -> None:
            # GIVEN the API returns two charms with no listing overrides
            client = self._find_client(find_names=["charm-a", "charm-b"])

            # WHEN finding charms
            result = client.find_charms()

            # THEN both charms are returned
            assert result == {"charm-a", "charm-b"}

        def test_listed_true_does_not_exclude_charm(self) -> None:
            # GIVEN the API returns a charm that also has listed: true
            client = self._find_client(
                find_names=["charm-a"],
                listed_by_charm={"charm-a": True},
            )

            # WHEN finding charms
            result = client.find_charms()

            # THEN the charm is still present
            assert "charm-a" in result
