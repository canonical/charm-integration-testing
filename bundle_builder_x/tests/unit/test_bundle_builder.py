# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for bundle_builder.py."""

from itertools import repeat
from typing import Iterator

from bundle_builder_x.assertion_tags import SubordinateBaseMismatchTag
from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointScope, EndpointType
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.charmhub_http import CharmReleaseNotFoundException
from bundle_builder_x.domain import (
    Domain,
    DomainApplication,
    DomainModel,
    ModelRef,
    add_charm_to_domain,
)
from bundle_builder_x.juju_version import JujuVersion

_JUJU = JujuVersion(major=3, minor=6, patch=0)
_CHANNEL = CharmChannel(track="latest", risk="stable", branch="")


class _FakeCharmhubClient(CharmhubClient):
    """Minimal typed stub for CharmhubClient, used in BundleBuilder unit tests."""

    def __init__(
        self,
        charm_responses: list[Charm | Exception] | Charm | Exception | None = None,
        find_result: set[str] | None = None,
    ) -> None:
        # Bypass CharmhubClient.__init__ - no HTTP client needed for unit tests.
        if charm_responses is None:
            self._responses: Iterator[Charm | Exception] = iter([])
        elif isinstance(charm_responses, list):
            self._responses = iter(charm_responses)
        else:
            # Single Charm or Exception: repeat indefinitely.
            self._responses = repeat(charm_responses)
        self._find_result: set[str] = find_result if find_result is not None else set()
        self.charm_from_store_calls: list[dict[str, object]] = []

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
        self.charm_from_store_calls.append(
            {
                "charm_name": charm_name,
                "ubuntu_arch": ubuntu_arch,
                "juju_version": juju_version,
                "platform": platform,
                "charm_track": charm_track,
                "charm_risk": charm_risk,
                "charm_revision": charm_revision,
                "ubuntu_version": ubuntu_version,
            }
        )
        resp = next(self._responses)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def find_charms(
        self,
        provides: str | None = None,
        requires: str | None = None,
        platform: str | None = None,
    ) -> set[str]:
        return self._find_result


def _make_charm(name: str, endpoints: dict[str, CharmEndpoint], ubuntu_version: str = "22.04") -> Charm:
    return Charm(
        name=name,
        channel=_CHANNEL,
        revision=1,
        ubuntu_version=ubuntu_version,
        ubuntu_arch="amd64",
        endpoints=endpoints,
    )


def _domain_with_base_mismatch() -> Domain:
    domain = Domain()
    domain.models[ModelRef(name="m")] = DomainModel(
        arch="amd64",
        platform="machine",
        juju_version=_JUJU,
        applications={
            "ubuntu": DomainApplication(charm="ubuntu"),
            "nrpe": DomainApplication(charm="nrpe"),
        },
    )
    add_charm_to_domain(
        _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope=EndpointScope.GLOBAL)},
            ubuntu_version="22.04",
        ),
        domain,
        ModelRef(name="m"),
    )
    add_charm_to_domain(
        _make_charm(
            "nrpe",
            {
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", scope=EndpointScope.CONTAINER
                )
            },
            ubuntu_version="24.04",
        ),
        domain,
        ModelRef(name="m"),
    )
    return domain


def _mismatch_tag() -> SubordinateBaseMismatchTag:
    return SubordinateBaseMismatchTag(
        subordinate_charm_name="nrpe",
        subordinate_charm_id=1,
        subordinate_endpoint="general-info",
        principal_charm_name="ubuntu",
        principal_charm_id=0,
        principal_endpoint="juju-info",
        subordinate_base="24.04",
        principal_base="22.04",
    )


class TestHandleSubordinateBaseMismatch:
    """BundleBuilder._handle_subordinate_base_mismatch."""

    def test_returns_true_and_expands_when_subordinate_variant_found(self) -> None:
        domain = _domain_with_base_mismatch()
        nrpe_2204 = _make_charm(
            "nrpe",
            {
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", scope=EndpointScope.CONTAINER
                )
            },
            ubuntu_version="22.04",
        )
        fake = _FakeCharmhubClient(
            charm_responses=[
                nrpe_2204,
                CharmReleaseNotFoundException("ubuntu", "No release on 24.04"),
            ]
        )
        builder = BundleBuilder(charmhub_client=fake)

        result = builder._handle_subordinate_base_mismatch(_mismatch_tag(), domain)

        assert result is True
        assert len(domain.charms) == 3
        assert domain.charms[2].spec.name == "nrpe"
        assert domain.charms[2].spec.ubuntu_version == "22.04"

    def test_returns_true_and_expands_when_principal_variant_found(self) -> None:
        domain = _domain_with_base_mismatch()
        ubuntu_2404 = _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope=EndpointScope.GLOBAL)},
            ubuntu_version="24.04",
        )
        fake = _FakeCharmhubClient(
            charm_responses=[
                CharmReleaseNotFoundException("nrpe", "No release on 22.04"),
                ubuntu_2404,
            ]
        )
        builder = BundleBuilder(charmhub_client=fake)

        result = builder._handle_subordinate_base_mismatch(_mismatch_tag(), domain)

        assert result is True
        assert len(domain.charms) == 3
        assert domain.charms[2].spec.name == "ubuntu"
        assert domain.charms[2].spec.ubuntu_version == "24.04"

    def test_returns_false_when_no_variant_found(self) -> None:
        domain = _domain_with_base_mismatch()
        fake = _FakeCharmhubClient(charm_responses=CharmReleaseNotFoundException("nrpe", "No release"))
        builder = BundleBuilder(charmhub_client=fake)

        result = builder._handle_subordinate_base_mismatch(_mismatch_tag(), domain)

        assert result is False
        assert len(domain.charms) == 2


class TestGetCharmsForEndpoint:
    """BundleBuilder._get_charms_for_endpoint: ubuntu_version forwarding for container-scoped endpoints."""

    def _domain_with_subordinate(self, ubuntu_version: str = "22.04") -> Domain:
        """Domain containing only a subordinate charm (requires juju-info, scope=container)."""
        domain = Domain()
        domain.models[ModelRef(name="m")] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
            applications={"nrpe": DomainApplication(charm="nrpe")},
        )
        add_charm_to_domain(
            _make_charm(
                "nrpe",
                {
                    "general-info": CharmEndpoint(
                        type=EndpointType.REQUIRES, interface="juju-info", scope=EndpointScope.CONTAINER
                    )
                },
                ubuntu_version=ubuntu_version,
            ),
            domain,
            ModelRef(name="m"),
        )
        return domain

    def _domain_with_global_endpoint(self) -> tuple[Domain, int]:
        """Domain containing a charm with a global-scope requires endpoint."""
        domain = Domain()
        domain.models[ModelRef(name="m")] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
            applications={"app": DomainApplication(charm="app")},
        )
        charm_id = add_charm_to_domain(
            _make_charm(
                "app", {"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql", scope=EndpointScope.GLOBAL)}
            ),
            domain,
            ModelRef(name="m"),
        )
        return domain, charm_id

    def test_container_scope_passes_ubuntu_version_to_charm_from_store(self) -> None:
        # GIVEN a subordinate charm on 22.04 with a container-scoped requires endpoint
        domain = self._domain_with_subordinate(ubuntu_version="22.04")
        ubuntu = _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope=EndpointScope.GLOBAL)},
        )
        fake = _FakeCharmhubClient(charm_responses=ubuntu, find_result={"ubuntu"})
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN fetching charms to fulfill the subordinate's container-scoped endpoint
        results = builder._get_charms_for_endpoint(0, "general-info", domain, ModelRef(name="m"))

        # THEN charm_from_store is called with ubuntu_version matching the subordinate's base
        assert results == [ubuntu]
        assert fake.charm_from_store_calls[0]["ubuntu_version"] == "22.04"

    def test_non_container_scope_passes_no_ubuntu_version(self) -> None:
        # GIVEN a charm with a global-scoped requires endpoint
        domain, charm_id = self._domain_with_global_endpoint()
        db = _make_charm("database", {"db": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql")})
        fake = _FakeCharmhubClient(charm_responses=db, find_result={"database"})
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN fetching charms for the global-scoped endpoint
        builder._get_charms_for_endpoint(charm_id, "db", domain, ModelRef(name="m"))

        # THEN charm_from_store is called with ubuntu_version=None (base irrelevant for global scope)
        assert fake.charm_from_store_calls[0]["ubuntu_version"] is None

    def test_container_scope_skips_charm_when_base_not_available(self) -> None:
        # GIVEN no principal charm exists at the subordinate's base
        domain = self._domain_with_subordinate(ubuntu_version="22.04")
        fake = _FakeCharmhubClient(
            charm_responses=CharmReleaseNotFoundException("ubuntu", "no 22.04 release"),
            find_result={"ubuntu"},
        )
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN fetching charms for the container-scoped endpoint
        results = builder._get_charms_for_endpoint(0, "general-info", domain, ModelRef(name="m"))

        # THEN the charm is skipped and the result is empty
        assert results == []
