# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for groundedness.py."""

import logging

from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.charmhub_http import CharmReleaseNotFoundException
from bundle_builder_x.domain import (
    Domain,
    DomainApplication,
    DomainApplicationEndpoint,
    DomainApplicationIntegration,
    DomainModel,
    ModelRef,
    add_charm_to_domain,
)
from bundle_builder_x.groundedness import find_unsatisfiable_endpoints, format_problems
from bundle_builder_x.juju_version import JujuVersion
from bundle_builder_x.overrides import OverridesClient

_JUJU = JujuVersion(major=3, minor=6, patch=0)
_CHANNEL = CharmChannel(track="latest", risk="stable", branch="")
_MODEL = ModelRef(name="m")


def _charm(name: str, **endpoints: CharmEndpoint) -> Charm:
    return Charm(
        name=name,
        channel=_CHANNEL,
        revision=1,
        ubuntu_version="24.04",
        ubuntu_arch="amd64",
        subordinate=False,
        endpoints=endpoints,
        priority=1.0,
        platforms=["machine"],
    )


def _requires(interface: str, optional: bool = False, cyclic: bool = False) -> CharmEndpoint:
    return CharmEndpoint(type=EndpointType.REQUIRES, interface=interface, optional=optional, cyclic=cyclic)


def _provides(interface: str, optional: bool = False, cyclic: bool = False) -> CharmEndpoint:
    return CharmEndpoint(type=EndpointType.PROVIDES, interface=interface, optional=optional, cyclic=cyclic)


def _peers(interface: str, optional: bool = False) -> CharmEndpoint:
    return CharmEndpoint(type=EndpointType.PEERS, interface=interface, optional=optional)


class _FakeClient(CharmhubClient):
    """Serves an in-memory catalogue, indexing providers/requirers by interface.

    `unlisted` mirrors the `listed: false` override (see e.g.
    static/charm-overrides/cinder.yaml): those charms are fetchable directly but
    excluded from find_charms, exactly like a real unlisted charm.
    """

    def __init__(self, catalog: dict[str, Charm], unlisted: set[str] | None = None) -> None:
        self._catalog = catalog
        self._unlisted = unlisted or set()
        self.overrides_client = OverridesClient()

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
        if charm_name not in self._catalog:
            raise CharmReleaseNotFoundException(f"no such charm: {charm_name}")
        return self._catalog[charm_name]

    def find_charms(
        self,
        provides: str | None = None,
        requires: str | None = None,
        platform: str | None = None,
    ) -> set[str]:
        wanted = EndpointType.PROVIDES if provides is not None else EndpointType.REQUIRES
        interface = provides if provides is not None else requires
        return {
            name
            for name, charm in self._catalog.items()
            if name not in self._unlisted
            and any(ep.type == wanted and ep.interface == interface for ep in charm.endpoints.values())
        }


def _domain(seed: Charm) -> Domain:
    domain = Domain()
    domain.models[_MODEL] = DomainModel(
        arch="amd64",
        platform="machine",
        juju_version=_JUJU,
        ref=_MODEL,
        applications={"app": DomainApplication(charm=seed.name)},
    )
    add_charm_to_domain(seed, domain, _MODEL)
    return domain


def _check(seed: Charm, catalog: dict[str, Charm]) -> list[str]:
    return find_unsatisfiable_endpoints(_FakeClient(catalog), _domain(seed), logging.getLogger("test"))


def _two_app_domain(app_a: Charm, app_b: Charm) -> Domain:
    domain = Domain()
    domain.models[_MODEL] = DomainModel(
        arch="amd64",
        platform="machine",
        juju_version=_JUJU,
        ref=_MODEL,
        applications={"a": DomainApplication(charm=app_a.name), "b": DomainApplication(charm=app_b.name)},
    )
    return domain


class TestFindUnsatisfiableEndpoints:
    """groundedness.find_unsatisfiable_endpoints."""

    def test_terminating_provider_chain_is_satisfiable(self) -> None:
        # GIVEN a charm requiring 'db', provided by a charm with no obligations of its own
        seed = _charm("app-charm", db=_requires("db"))
        catalog = {"app-charm": seed, "postgresql": _charm("postgresql", db=_provides("db"))}

        # WHEN the spec is checked
        # THEN no proof of unsatisfiability is found
        assert _check(seed, catalog) == []

    def test_mutually_dependent_providers_are_unsatisfiable(self) -> None:
        # GIVEN the only provider of 'db' also non-optionally requires 'db', so every
        # candidate partner reintroduces the same obligation
        seed = _charm("app-charm", db=_requires("db"))
        mesh = _charm("mesh", need=_requires("db"), give=_provides("db"))
        catalog = {"app-charm": seed, "mesh": mesh}

        # WHEN the spec is checked
        problems = _check(seed, catalog)

        # THEN the offending endpoint is reported
        assert len(problems) == 1
        assert "app-charm:db" in problems[0]
        assert "'db'" in problems[0]

    def test_missing_provider_is_unsatisfiable(self) -> None:
        # GIVEN nothing at all provides the required interface
        seed = _charm("app-charm", db=_requires("db"))

        # WHEN the spec is checked
        problems = _check(seed, {"app-charm": seed})

        # THEN the endpoint is reported as unsatisfiable
        assert len(problems) == 1
        assert "app-charm:db" in problems[0]

    def test_optional_endpoint_is_ignored(self) -> None:
        # GIVEN the unprovidable endpoint is optional
        seed = _charm("app-charm", db=_requires("db", optional=True))

        # WHEN the spec is checked
        # THEN it is not reported, since the solver may leave it unintegrated
        assert _check(seed, {"app-charm": seed}) == []

    def test_cyclic_endpoint_is_ignored(self) -> None:
        # GIVEN the endpoint is flagged cyclic, exempting it from the rank ordering
        seed = _charm("app-charm", db=_requires("db", cyclic=True))

        # WHEN the spec is checked
        # THEN it is not reported
        assert _check(seed, {"app-charm": seed}) == []

    def test_non_optional_peer_endpoint_is_unsatisfiable(self) -> None:
        # GIVEN a non-optional peer endpoint, which never receives an integration variable
        seed = _charm("app-charm", cluster=_peers("cluster"))

        # WHEN the spec is checked
        problems = _check(seed, {"app-charm": seed})

        # THEN it is reported as impossible to integrate
        assert len(problems) == 1
        assert "peer endpoint" in problems[0]

    def test_optional_peer_endpoint_is_ignored(self) -> None:
        # GIVEN an optional peer endpoint
        seed = _charm("app-charm", cluster=_peers("cluster", optional=True))

        # WHEN the spec is checked
        # THEN it is not reported
        assert _check(seed, {"app-charm": seed}) == []

    def test_alternative_grounded_provider_rescues_the_chain(self) -> None:
        # GIVEN one provider of 'db' is self-referential but another terminates
        seed = _charm("app-charm", db=_requires("db"))
        catalog = {
            "app-charm": seed,
            "mesh": _charm("mesh", need=_requires("db"), give=_provides("db")),
            "postgresql": _charm("postgresql", db=_provides("db")),
        }

        # WHEN the spec is checked
        # THEN the spec is not rejected, because a terminating chain exists
        assert _check(seed, catalog) == []

    def test_provider_grounded_through_a_second_interface(self) -> None:
        # GIVEN the provider of 'db' itself requires 'tls', which is satisfiable
        seed = _charm("app-charm", db=_requires("db"))
        catalog = {
            "app-charm": seed,
            "postgresql": _charm("postgresql", db=_provides("db"), tls=_requires("tls")),
            "ca": _charm("ca", tls=_provides("tls")),
        }

        # WHEN the spec is checked
        # THEN no proof of unsatisfiability is found
        assert _check(seed, catalog) == []

    def test_unsatisfiable_second_hop_is_reported(self) -> None:
        # GIVEN the only provider of 'db' requires 'tls', which nothing provides
        seed = _charm("app-charm", db=_requires("db"))
        catalog = {
            "app-charm": seed,
            "postgresql": _charm("postgresql", db=_provides("db"), tls=_requires("tls")),
        }

        # WHEN the spec is checked
        # THEN the seed endpoint is reported, since its chain cannot terminate
        assert len(_check(seed, catalog)) == 1

    def test_non_optional_provides_needs_a_requirer(self) -> None:
        # GIVEN a charm that non-optionally provides an interface nothing requires
        seed = _charm("app-charm", metrics=_provides("metrics"))

        # WHEN the spec is checked
        # THEN the endpoint is reported, since it can never reach its required count
        assert len(_check(seed, {"app-charm": seed})) == 1

    def test_pinned_release_is_read_instead_of_the_default(self) -> None:
        # GIVEN an application pinned to a revision whose endpoint is optional, while the
        # default release of the same charm makes it non-optional and unprovidable
        pinned = _charm("app-charm", db=_requires("db", optional=True))
        default = _charm("app-charm", db=_requires("db"))

        class _PinnedClient(_FakeClient):
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
                return pinned if charm_revision == 7 else default

        domain = _domain(default)
        domain.models[_MODEL].applications["app"].revision = 7

        # GIVEN reading the default release instead would reject the spec
        assert len(_check(default, {"app-charm": default})) == 1

        # WHEN the spec is checked
        problems = find_unsatisfiable_endpoints(
            _PinnedClient({"app-charm": default}), domain, logging.getLogger("test")
        )

        # THEN the pinned release is what is judged, so the spec is not rejected
        assert problems == []

    def test_cyclic_partner_endpoint_discharges_the_obligation(self) -> None:
        # GIVEN the only provider of 'db' also non-optionally requires 'db', but marks its
        # provides side cyclic - as temporal-k8s/temporal-ui-k8s do in static/charm-overrides.
        # add_charm_dependency_constraints skips the rank ordering when either side is
        # cyclic, so the solver is free to close the loop.
        seed = _charm("app-charm", db=_requires("db"))
        mesh = _charm("mesh", need=_requires("db"), give=_provides("db", cyclic=True))

        # GIVEN the same shape without the cyclic marker is rejected
        plain = _charm("mesh", need=_requires("db"), give=_provides("db"))
        assert len(_check(seed, {"app-charm": seed, "mesh": plain})) == 1

        # WHEN the spec is checked
        # THEN the cyclic partner keeps it satisfiable
        assert _check(seed, {"app-charm": seed, "mesh": mesh}) == []

    def test_external_cross_model_integration_satisfies_the_endpoint(self) -> None:
        # GIVEN a non-optional requires with no provider anywhere in the catalogue,
        # wired instead to an offer in a model outside the domain
        seed = _charm("app-charm", db=_requires("db"))
        domain = _domain(seed)
        domain.models[_MODEL].application_integrations.append(
            DomainApplicationIntegration(
                endpoint_1=DomainApplicationEndpoint(application="app", endpoint="db"),
                endpoint_2=DomainApplicationEndpoint(application="remote", endpoint="db", model=ModelRef(name="other")),
                url="admin/other.remote",
            )
        )

        # GIVEN the same spec without the cross-model integration is rejected
        assert len(_check(seed, {"app-charm": seed})) == 1

        # WHEN the spec is checked
        problems = find_unsatisfiable_endpoints(_FakeClient({"app-charm": seed}), domain, logging.getLogger("test"))

        # THEN the endpoint is left alone, since add_charm_constraints adds a count term for it
        assert problems == []

    def test_juju_info_is_never_reported(self) -> None:
        # GIVEN a subordinate whose non-optional juju-info requirement no charm declares,
        # because principals provide it implicitly rather than in metadata
        seed = _charm("sub-charm", info=_requires("juju-info"))

        # WHEN the spec is checked
        # THEN it is not reported
        assert _check(seed, {"sub-charm": seed}) == []

    def test_missing_provider_says_so(self) -> None:
        # GIVEN nothing at all provides the required interface
        seed = _charm("app-charm", db=_requires("db"))

        # WHEN the spec is checked
        # THEN the message names the real cause rather than blaming a non-terminating chain
        assert "no charm providing 'db' is available" in _check(seed, {"app-charm": seed})[0]

    def test_unlisted_seed_still_grounds_another_seeds_obligation(self) -> None:
        # GIVEN two applications explicitly named in the spec, where the only real-world
        # provider of one's obligation is a charm the `listed: false` override (e.g.
        # static/charm-overrides/cinder.yaml) excludes from find_charms. A seed is never
        # discovered via find_charms - it is named explicitly - so it must still be
        # usable as another seed's partner regardless of `listed`.
        subordinate = _charm("cinder-lvm", storage=_provides("cinder-backend"))
        principal = _charm("cinder", storage=_requires("cinder-backend", optional=True))
        domain = _two_app_domain(subordinate, principal)

        # WHEN checked against a client that hides "cinder" from find_charms, exactly as
        # the real Charmhub client does for an unlisted charm
        problems = find_unsatisfiable_endpoints(
            _FakeClient({"cinder-lvm": subordinate, "cinder": principal}, unlisted={"cinder"}),
            domain,
            logging.getLogger("test"),
        )

        # THEN cinder-lvm's obligation is not rejected, since cinder is right there in the spec
        assert problems == []

    def test_pinned_seed_partner_uses_its_resolved_release(self) -> None:
        # GIVEN two applications pin different releases of the same charm, and only one
        # pinned release provides db without introducing an ungrounded dependency
        consumer = _charm("consumer", db=_requires("db"))
        grounded_provider = _charm("provider", db=_provides("db"))
        other_pinned_release = _charm("provider", metrics=_provides("metrics", optional=True))
        default_provider = _charm("provider", db=_provides("db"), tls=_requires("tls"))

        class _PinnedPartnerClient(_FakeClient):
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
                if charm_name == "provider" and charm_revision == 7:
                    return grounded_provider
                if charm_name == "provider" and charm_revision == 8:
                    return other_pinned_release
                if charm_name == "provider":
                    return default_provider
                return super().charm_from_store(
                    charm_name,
                    ubuntu_arch,
                    juju_version,
                    platform,
                    charm_track,
                    charm_risk,
                    charm_revision,
                    ubuntu_version,
                )

        domain = Domain()
        domain.models[_MODEL] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
            ref=_MODEL,
            applications={
                "consumer": DomainApplication(charm=consumer.name),
                "grounded": DomainApplication(charm=grounded_provider.name, revision=7),
                "other-release": DomainApplication(charm=other_pinned_release.name, revision=8),
            },
        )
        client = _PinnedPartnerClient(
            {"consumer": consumer, "provider": default_provider},
            unlisted={"provider"},
        )

        # GIVEN resolving the provider by name would use the ungrounded default release
        assert len(_check(consumer, {"consumer": consumer, "provider": default_provider})) == 1

        # WHEN the spec is checked
        problems = find_unsatisfiable_endpoints(client, domain, logging.getLogger("test"))

        # THEN the pinned, grounded release is retained as a distinct partner
        assert problems == []


class TestFormatProblems:
    """groundedness.format_problems."""

    def test_lists_every_problem_when_few(self) -> None:
        assert format_problems(["a", "b"]) == "a; b"

    def test_caps_the_listing_and_counts_the_rest(self) -> None:
        formatted = format_problems([str(i) for i in range(8)])
        assert formatted.startswith("0; 1; 2; 3; 4")
        assert formatted.endswith("(and 3 more)")
