# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import yaml

from bundle_builder_x.bundle import (
    Application,
    ApplicationEndpoint,
    Bundle,
    CrossModelIntegration,
    Solution,
)
from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.juju_version import JujuVersion


def _make_charm(name: str, endpoints: dict[str, CharmEndpoint] | None = None) -> Charm:
    """Helper to create a minimal Charm for testing."""
    return Charm(
        name=name,
        channel=CharmChannel(track="1", risk="stable", branch=""),
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=endpoints or {},
        platforms=["machine", "kubernetes"],
    )


def _make_bundle(**kwargs: object) -> Bundle:
    """Helper to create a Bundle with sensible defaults."""
    defaults = {
        "applications": {},
        "integrations": set(),
        "cross_model_integrations": [],
        "platform": "kubernetes",
        "arch": "amd64",
        "juju_version": JujuVersion(major=3, minor=6, patch=0),
    }
    defaults.update(kwargs)
    return Bundle(**defaults)


class TestBundleExportOffers:
    def test_provides_side_emits_offers_under_application(self) -> None:
        # GIVEN a bundle where the local app provides an endpoint via CMR
        charm = _make_charm(
            "postgresql",
            endpoints={"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql")},
        )
        bundle = _make_bundle(
            applications={"postgresql": Application(charm=charm)},
            platform="machine",
            cross_model_integrations=[
                CrossModelIntegration(
                    local=ApplicationEndpoint(application="postgresql", endpoint="database"),
                    local_role=EndpointType.PROVIDES,
                    remote_model="model-a",
                    remote_application="db-proxy",
                    remote_endpoint="database",
                    offer_name="postgresql-offer",
                ),
            ],
        )

        # WHEN exporting
        documents = list(yaml.safe_load_all(bundle.export()))
        base = documents[0]
        overlay = documents[1]

        # THEN the base application has no offers
        assert "offers" not in base["applications"]["postgresql"]

        # AND offers are in the overlay document
        app = overlay["applications"]["postgresql"]
        assert "offers" in app
        assert "postgresql-offer" in app["offers"]
        assert app["offers"]["postgresql-offer"]["endpoints"] == ["database"]

    def test_requires_side_emits_saas(self) -> None:
        # GIVEN a bundle where the local app requires an endpoint via CMR
        charm = _make_charm(
            "pgbouncer-k8s",
            endpoints={"database": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql")},
        )
        bundle = _make_bundle(
            applications={"db-proxy": Application(charm=charm)},
            cross_model_integrations=[
                CrossModelIntegration(
                    local=ApplicationEndpoint(application="db-proxy", endpoint="database"),
                    local_role=EndpointType.REQUIRES,
                    remote_model="model-b",
                    remote_application="postgresql",
                    remote_endpoint="database",
                    offer_name="postgresql-offer",
                    url="lxd:admin/model-b.postgresql-offer",
                ),
            ],
        )

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN saas section exists with the offer_name as key
        assert "saas" in exported
        assert "postgresql-offer" in exported["saas"]
        assert exported["saas"]["postgresql-offer"]["url"] == "lxd:admin/model-b.postgresql-offer"

        # AND relations include the CMR relation using the offer name
        relations = exported["relations"]
        assert ["db-proxy:database", "postgresql-offer:database"] in relations

    def test_multiple_endpoints_coalesce_into_single_offer(self) -> None:
        # GIVEN two CMR integrations on the same app with the same offer_name
        charm = _make_charm(
            "postgresql",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql"),
                "replication": CharmEndpoint(type=EndpointType.PROVIDES, interface="replication"),
            },
        )
        bundle = _make_bundle(
            applications={"postgresql": Application(charm=charm)},
            cross_model_integrations=[
                CrossModelIntegration(
                    local=ApplicationEndpoint(application="postgresql", endpoint="database"),
                    local_role=EndpointType.PROVIDES,
                    remote_model="model-a",
                    remote_application="proxy",
                    remote_endpoint="database",
                    offer_name="postgresql-offer",
                ),
                CrossModelIntegration(
                    local=ApplicationEndpoint(application="postgresql", endpoint="replication"),
                    local_role=EndpointType.PROVIDES,
                    remote_model="model-a",
                    remote_application="proxy",
                    remote_endpoint="replication",
                    offer_name="postgresql-offer",
                ),
            ],
        )

        # WHEN exporting
        documents = list(yaml.safe_load_all(bundle.export()))
        overlay = documents[1]

        # THEN the single offer in the overlay has both endpoints
        offer = overlay["applications"]["postgresql"]["offers"]["postgresql-offer"]
        assert sorted(offer["endpoints"]) == ["database", "replication"]

    def test_no_cmr_produces_no_saas_or_offers(self) -> None:
        # GIVEN a bundle with no cross-model integrations
        charm = _make_charm("my-charm")
        bundle = _make_bundle(
            applications={"my-app": Application(charm=charm)},
        )

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN no saas or offers sections exist
        assert "saas" not in exported
        for app in exported["applications"].values():
            assert "offers" not in app


class TestSolutionExportMermaidCMR:
    def test_cmr_adds_dashed_edges_across_subgraphs(self) -> None:
        # GIVEN two bundles connected by a CMR
        provider_charm = _make_charm(
            "postgresql",
            endpoints={"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql")},
        )
        requirer_charm = _make_charm(
            "pgbouncer-k8s",
            endpoints={"backend-database": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql")},
        )
        model_a = _make_bundle(
            model="model-a",
            applications={"postgresql": Application(charm=provider_charm)},
            cross_model_integrations=[
                CrossModelIntegration(
                    local=ApplicationEndpoint(application="postgresql", endpoint="database"),
                    local_role=EndpointType.PROVIDES,
                    remote_model="model-b",
                    remote_application="db-proxy",
                    remote_endpoint="backend-database",
                    offer_name="postgresql-offer",
                    url=None,
                ),
            ],
        )
        model_b = _make_bundle(
            model="model-b",
            applications={"db-proxy": Application(charm=requirer_charm)},
            cross_model_integrations=[
                CrossModelIntegration(
                    local=ApplicationEndpoint(application="db-proxy", endpoint="backend-database"),
                    local_role=EndpointType.REQUIRES,
                    remote_model="model-a",
                    remote_application="postgresql",
                    remote_endpoint="database",
                    offer_name="postgresql-offer",
                    url="lxd:admin/model-a.postgresql-offer",
                ),
            ],
        )
        solution = Solution(bundles=[model_a, model_b])

        # WHEN exporting mermaid
        mermaid = solution.export_mermaid()

        # THEN both models appear as subgraphs
        assert "subgraph model-a" in mermaid
        assert "subgraph model-b" in mermaid

        # AND cross-model dashed edge is present
        assert "-.->|" in mermaid

        # AND node IDs are namespaced
        assert "model-a__postgresql" in mermaid
        assert "model-b__db-proxy" in mermaid
