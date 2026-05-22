# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for global multi-model domain behavior."""

from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.domain import (
    Domain,
    DomainApplication,
    DomainApplicationEndpoint,
    DomainApplicationIntegration,
    DomainModel,
    ModelRef,
    add_charm_to_domain,
)
from bundle_builder_x.juju_version import JujuVersion


def _make_domain(models: dict[ModelRef, DomainModel]) -> Domain:
    domain = Domain()
    domain.models.update(models)
    return domain


def _make_charm(
    name: str,
    endpoints: dict[str, CharmEndpoint] | None = None,
) -> Charm:
    return Charm(
        name=name,
        channel=CharmChannel(track="1", risk="stable", branch=""),
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=endpoints or {},
    )


_JUJU = JujuVersion(major=3, minor=6, patch=0)


class TestInitializeGlobalDomain:
    def test_creates_models(self) -> None:
        # GIVEN two models
        domain = _make_domain(
            {
                ModelRef(name="k8s"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"app-a": DomainApplication(charm="charm-a")},
                ),
                ModelRef(name="machine"): DomainModel(
                    arch="amd64",
                    platform="machine",
                    juju_version=_JUJU,
                    applications={"app-b": DomainApplication(charm="charm-b")},
                ),
            }
        )

        # THEN both models are in the domain
        assert set(domain.models) == {ModelRef(name="k8s"), ModelRef(name="machine")}
        assert domain.models[ModelRef(name="k8s")].platform == "kubernetes"
        assert domain.models[ModelRef(name="machine")].platform == "machine"

    def test_global_charms_list_starts_empty(self) -> None:
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(arch="amd64", platform="kubernetes", juju_version=_JUJU),
            }
        )
        assert len(domain.charms) == 0


class TestAddCharmCrossModelPairing:
    def test_same_model_creates_local_integration(self) -> None:
        # GIVEN a domain with one model
        domain = _make_domain(
            {
                ModelRef(name="m1"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"pg": DomainApplication(charm="postgresql")},
                ),
            }
        )

        # WHEN adding two charms with matching interfaces to the same model
        pg = _make_charm(
            "postgresql",
            {
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql"),
            },
        )
        proxy = _make_charm(
            "pgbouncer",
            {
                "backend-database": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql"),
            },
        )
        add_charm_to_domain(pg, domain, ModelRef(name="m1"))
        add_charm_to_domain(proxy, domain, ModelRef(name="m1"))

        # THEN a local DomainCharmIntegration is created (no cross-model)
        assert len(domain.charm_integrations) == 1
        assert not domain.is_cross_model(domain.charm_integrations[0])

    def test_cross_model_creates_cross_model_integration(self) -> None:
        # GIVEN a domain with two models
        domain = _make_domain(
            {
                ModelRef(name="k8s"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"proxy": DomainApplication(charm="pgbouncer")},
                ),
                ModelRef(name="machine"): DomainModel(
                    arch="amd64",
                    platform="machine",
                    juju_version=_JUJU,
                    applications={"pg": DomainApplication(charm="postgresql")},
                ),
            }
        )

        # WHEN adding charms with matching interfaces to different models
        pg = _make_charm(
            "postgresql",
            {
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql"),
            },
        )
        proxy = _make_charm(
            "pgbouncer",
            {
                "backend-database": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql"),
            },
        )
        add_charm_to_domain(pg, domain, ModelRef(name="machine"))
        add_charm_to_domain(proxy, domain, ModelRef(name="k8s"))

        # THEN no local integration, but a cross-model DomainCharmIntegration is created
        assert len(domain.charm_integrations) == 1
        assert domain.is_cross_model(domain.charm_integrations[0])

        cmr_integration = domain.charm_integrations[0]
        assert domain.charms[cmr_integration.provides_charm_id].model.key == "machine"
        assert domain.charms[cmr_integration.requires_charm_id].model.key == "k8s"
        assert domain.integration_interface(cmr_integration) == "postgresql"

    def test_no_potential_cmr_for_mismatched_interfaces(self) -> None:
        # GIVEN two models with charms that have different interfaces
        domain = _make_domain(
            {
                ModelRef(name="m1"): DomainModel(arch="amd64", platform="kubernetes", juju_version=_JUJU),
                ModelRef(name="m2"): DomainModel(arch="amd64", platform="machine", juju_version=_JUJU),
            }
        )

        charm_a = _make_charm(
            "charm-a",
            {
                "ep-a": CharmEndpoint(type=EndpointType.PROVIDES, interface="http"),
            },
        )
        charm_b = _make_charm(
            "charm-b",
            {
                "ep-b": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql"),
            },
        )
        add_charm_to_domain(charm_a, domain, ModelRef(name="m1"))
        add_charm_to_domain(charm_b, domain, ModelRef(name="m2"))

        # THEN no cross-model integration
        assert all(not domain.is_cross_model(i) for i in domain.charm_integrations)

    def test_charm_model_name_tracking(self) -> None:
        # GIVEN a domain with two models
        domain = _make_domain(
            {
                ModelRef(name="m1"): DomainModel(arch="amd64", platform="kubernetes", juju_version=_JUJU),
                ModelRef(name="m2"): DomainModel(arch="amd64", platform="machine", juju_version=_JUJU),
            }
        )

        charm_a = _make_charm("charm-a")
        charm_b = _make_charm("charm-b")
        id_a = add_charm_to_domain(charm_a, domain, ModelRef(name="m1"))
        id_b = add_charm_to_domain(charm_b, domain, ModelRef(name="m2"))

        # THEN model is tracked on each DomainCharm
        assert domain.charms[id_a].model.key == "m1"
        assert domain.charms[id_b].model.key == "m2"

    def test_application_mappings_scoped_to_model(self) -> None:
        # GIVEN a domain with two models, each with a charm named "postgresql"
        domain = _make_domain(
            {
                ModelRef(name="m1"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"pg-1": DomainApplication(charm="postgresql")},
                ),
                ModelRef(name="m2"): DomainModel(
                    arch="amd64",
                    platform="machine",
                    juju_version=_JUJU,
                    applications={"pg-2": DomainApplication(charm="postgresql")},
                ),
            }
        )

        pg = _make_charm(
            "postgresql",
            {
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql"),
            },
        )
        add_charm_to_domain(pg, domain, ModelRef(name="m1"))
        add_charm_to_domain(pg, domain, ModelRef(name="m2"))

        # THEN each model has exactly one application with a charm mapping
        assert len(domain.models[ModelRef(name="m1")].applications) == 1
        assert len(domain.models[ModelRef(name="m1")].applications["pg-1"].charm_ids) == 1
        assert len(domain.models[ModelRef(name="m2")].applications) == 1
        assert len(domain.models[ModelRef(name="m2")].applications["pg-2"].charm_ids) == 1


def _cmr_mapping_count(domain: object, model_ref: ModelRef) -> int:
    """Count application_integrations entries that are cross-model and have charm mappings."""
    mc = domain.models[model_ref]  # type: ignore[attr-defined]
    return sum(
        1
        for app_int in mc.application_integrations
        if isinstance(app_int, DomainApplicationIntegration)
        and app_int.endpoint_1.model != app_int.endpoint_2.model
        and len(app_int.charm_integration_ids) > 0
    )


class TestCMRIntegrationMapping:
    """Unit tests for CMR entries in charm_integration_ids on DomainApplicationIntegration.

    CMR entries have DomainApplicationIntegration objects where at least one
    endpoint has a non-None ``model`` field.  Their charm_integration_ids are
    populated lazily as charms are added to the domain.
    """

    def test_mapping_created_after_both_charms_added(self) -> None:
        # GIVEN a two-model domain where consumer-model declares a user CMR to provider-model
        provider = _make_charm(
            "postgresql",
            {"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql")},
        )
        consumer = _make_charm(
            "pgbouncer",
            {"backend-database": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql")},
        )
        domain = _make_domain(
            {
                ModelRef(name="provider-model"): DomainModel(
                    arch="amd64",
                    platform="machine",
                    juju_version=_JUJU,
                    applications={"pg": DomainApplication(charm="postgresql")},
                ),
                ModelRef(name="consumer-model"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"proxy": DomainApplication(charm="pgbouncer")},
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="proxy", endpoint="backend-database"),
                            endpoint_2=DomainApplicationEndpoint(
                                application="pg", endpoint="database", model=ModelRef(name="provider-model")
                            ),
                            offer_name="pg-offer",
                        )
                    ],
                ),
            }
        )

        # WHEN only the local (consumer) charm is added
        add_charm_to_domain(consumer, domain, ModelRef(name="consumer-model"))

        # THEN no CMR mapping exists yet
        assert _cmr_mapping_count(domain, ModelRef(name="consumer-model")) == 0

        # WHEN the remote (provider) charm is also added
        add_charm_to_domain(provider, domain, ModelRef(name="provider-model"))

        # THEN exactly one CMR mapping is created for the consumer-model
        assert _cmr_mapping_count(domain, ModelRef(name="consumer-model")) == 1

        # Verify the mapping points to a cross-model integration
        mc = domain.models[ModelRef(name="consumer-model")]
        for app_int in mc.application_integrations:
            if app_int.endpoint_1.model != app_int.endpoint_2.model:
                for i_idx in app_int.charm_integration_ids:
                    assert domain.is_cross_model(domain.charm_integrations[i_idx])

    def test_mapping_created_regardless_of_charm_addition_order(self) -> None:
        # GIVEN the same two-model setup as above, but provider charm added first
        provider = _make_charm(
            "postgresql",
            {"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql")},
        )
        consumer = _make_charm(
            "pgbouncer",
            {"backend-database": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql")},
        )
        domain = _make_domain(
            {
                ModelRef(name="provider-model"): DomainModel(
                    arch="amd64",
                    platform="machine",
                    juju_version=_JUJU,
                    applications={"pg": DomainApplication(charm="postgresql")},
                ),
                ModelRef(name="consumer-model"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"proxy": DomainApplication(charm="pgbouncer")},
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="proxy", endpoint="backend-database"),
                            endpoint_2=DomainApplicationEndpoint(
                                application="pg", endpoint="database", model=ModelRef(name="provider-model")
                            ),
                            offer_name="pg-offer",
                        )
                    ],
                ),
            }
        )

        # WHEN the provider charm is added first (reverse order)
        add_charm_to_domain(provider, domain, ModelRef(name="provider-model"))

        # THEN still no CMR mapping yet
        assert _cmr_mapping_count(domain, ModelRef(name="consumer-model")) == 0

        # WHEN the consumer charm is added second
        add_charm_to_domain(consumer, domain, ModelRef(name="consumer-model"))

        # THEN the mapping is created correctly
        assert _cmr_mapping_count(domain, ModelRef(name="consumer-model")) == 1

    def test_mapping_not_created_for_external_cmr(self) -> None:
        # GIVEN a single-model domain with a CMR pointing to a model not in the domain
        consumer = _make_charm(
            "pgbouncer",
            {"backend-database": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql")},
        )
        domain = _make_domain(
            {
                ModelRef(name="consumer-model"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"proxy": DomainApplication(charm="pgbouncer")},
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="proxy", endpoint="backend-database"),
                            endpoint_2=DomainApplicationEndpoint(
                                application="pg", endpoint="database", model=ModelRef(name="external-model")
                            ),
                            offer_name="pg-offer",
                            url="lxd:admin/external-model.pg-offer",
                        )
                    ],
                ),
            }
        )

        # WHEN the charm is added
        add_charm_to_domain(consumer, domain, ModelRef(name="consumer-model"))

        # THEN no CMR mapping is created: external CMRs have no DomainCharmIntegration to map to
        assert _cmr_mapping_count(domain, ModelRef(name="consumer-model")) == 0


class TestAddCharmToDomainContainerScopeGating:
    """add_charm_to_domain: container-scoped integrations are gated to same-model machine platforms."""

    def test_container_scope_integration_created_on_machine_model(self) -> None:
        domain = _make_domain({ModelRef(name="m"): DomainModel(arch="amd64", platform="machine", juju_version=_JUJU)})
        principal = _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope="global")},
        )
        subordinate = _make_charm(
            "nrpe",
            {"general-info": CharmEndpoint(type=EndpointType.REQUIRES, interface="juju-info", scope="container")},
        )
        add_charm_to_domain(principal, domain, ModelRef(name="m"))
        add_charm_to_domain(subordinate, domain, ModelRef(name="m"))

        juju_info = [i for i in domain.charm_integrations if domain.integration_interface(i) == "juju-info"]
        assert len(juju_info) == 1

    def test_container_scope_integration_not_created_on_k8s_model(self) -> None:
        domain = _make_domain(
            {ModelRef(name="k"): DomainModel(arch="amd64", platform="kubernetes", juju_version=_JUJU)}
        )
        principal = _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope="global")},
        )
        subordinate = _make_charm(
            "nrpe",
            {"general-info": CharmEndpoint(type=EndpointType.REQUIRES, interface="juju-info", scope="container")},
        )
        add_charm_to_domain(principal, domain, ModelRef(name="k"))
        add_charm_to_domain(subordinate, domain, ModelRef(name="k"))

        juju_info = [i for i in domain.charm_integrations if domain.integration_interface(i) == "juju-info"]
        assert len(juju_info) == 0

    def test_non_container_scope_interface_not_gated_on_k8s(self) -> None:
        domain = _make_domain(
            {ModelRef(name="k"): DomainModel(arch="amd64", platform="kubernetes", juju_version=_JUJU)}
        )
        charm_a = _make_charm("app", {"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql")})
        charm_b = _make_charm("pg", {"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql")})
        add_charm_to_domain(charm_a, domain, ModelRef(name="k"))
        add_charm_to_domain(charm_b, domain, ModelRef(name="k"))

        assert len([i for i in domain.charm_integrations if domain.integration_interface(i) == "pgsql"]) == 1

    def test_non_juju_info_container_scope_also_gated_on_k8s(self) -> None:
        # container scope gating applies to any interface, not just juju-info
        domain = _make_domain(
            {ModelRef(name="k"): DomainModel(arch="amd64", platform="kubernetes", juju_version=_JUJU)}
        )
        principal = _make_charm(
            "app",
            {"custom-sub": CharmEndpoint(type=EndpointType.PROVIDES, interface="custom-sub-iface", scope="global")},
        )
        subordinate = _make_charm(
            "sub",
            {"custom-sub": CharmEndpoint(type=EndpointType.REQUIRES, interface="custom-sub-iface", scope="container")},
        )
        add_charm_to_domain(principal, domain, ModelRef(name="k"))
        add_charm_to_domain(subordinate, domain, ModelRef(name="k"))

        assert len(domain.charm_integrations) == 0

    def test_container_scope_cross_model_blocked_mixed_platform(self) -> None:
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(arch="amd64", platform="machine", juju_version=_JUJU),
                ModelRef(name="k"): DomainModel(arch="amd64", platform="kubernetes", juju_version=_JUJU),
            }
        )
        principal = _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope="global")},
        )
        subordinate = _make_charm(
            "nrpe",
            {"general-info": CharmEndpoint(type=EndpointType.REQUIRES, interface="juju-info", scope="container")},
        )
        add_charm_to_domain(principal, domain, ModelRef(name="m"))
        add_charm_to_domain(subordinate, domain, ModelRef(name="k"))

        juju_info = [i for i in domain.charm_integrations if domain.integration_interface(i) == "juju-info"]
        assert len(juju_info) == 0

    def test_container_scope_cross_model_blocked_both_machine(self) -> None:
        domain = _make_domain(
            {
                ModelRef(name="m1"): DomainModel(arch="amd64", platform="machine", juju_version=_JUJU),
                ModelRef(name="m2"): DomainModel(arch="amd64", platform="machine", juju_version=_JUJU),
            }
        )
        principal = _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope="global")},
        )
        subordinate = _make_charm(
            "nrpe",
            {"general-info": CharmEndpoint(type=EndpointType.REQUIRES, interface="juju-info", scope="container")},
        )
        add_charm_to_domain(principal, domain, ModelRef(name="m1"))
        add_charm_to_domain(subordinate, domain, ModelRef(name="m2"))

        juju_info = [i for i in domain.charm_integrations if domain.integration_interface(i) == "juju-info"]
        assert len(juju_info) == 0
