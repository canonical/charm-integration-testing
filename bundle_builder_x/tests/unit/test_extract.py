# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for extract.py: extracting a Solution from a Z3 model."""

import logging

import yaml
import z3  # type: ignore[import-untyped]

from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.constraints import add_constraints
from bundle_builder_x.domain import (
    Domain,
    DomainApplication,
    DomainApplicationEndpoint,
    DomainApplicationIntegration,
    DomainModel,
    ModelRef,
)
from bundle_builder_x.extract import extract_solution
from bundle_builder_x.juju_version import JujuVersion

_JUJU = JujuVersion(major=3, minor=6, patch=0)
_LOGGER = logging.getLogger("test_extract")


def _make_domain(models: dict[ModelRef, DomainModel]) -> Domain:
    domain = Domain()
    domain.models.update(models)
    return domain


def _make_charm(
    name: str,
    endpoints: dict[str, CharmEndpoint] | None = None,
    channel: str = "stable",
    revision: int = 1,
    config_defaults: dict[str, object] | None = None,
) -> Charm:
    return Charm(
        name=name,
        channel=CharmChannel.model_validate(channel),
        revision=revision,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=endpoints or {},
        config_defaults=config_defaults or {},
        platforms=["machine", "kubernetes"],
    )


def _solve(domain: object) -> z3.ModelRef:
    """Run the solver on a domain and return the Z3 model."""
    solver = z3.Solver()
    add_constraints(solver, domain)  # type: ignore[arg-type]
    assert solver.check() == z3.sat, f"Expected SAT but got {solver.check()}"
    return solver.model()


class TestExtractSingleModel:
    def test_pinned_app_maps_to_named_application(self) -> None:
        # GIVEN a domain with one pinned application
        from bundle_builder_x.domain import add_charm_to_domain

        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"my-pg": DomainApplication(charm="postgresql-k8s")},
                )
            }
        )
        charm = _make_charm(
            "postgresql-k8s",
            endpoints={"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql", optional=True)},
        )
        add_charm_to_domain(charm, domain, ModelRef(name="m"))

        # WHEN extracting
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN the application name matches the spec
        assert "my-pg" in solution.bundles[0].applications
        # AND the charm name is correct
        assert solution.bundles[0].applications["my-pg"].charm.name == "postgresql-k8s"

    def test_auto_discovered_charm_gets_charm_name(self) -> None:
        # GIVEN a domain where a charm exists but has no application constraint mapping
        from bundle_builder_x.domain import add_charm_to_domain

        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                )
            }
        )
        charm = _make_charm("grafana-agent-k8s")
        add_charm_to_domain(charm, domain, ModelRef(name="m"))
        # Force the charm to exist
        solver = z3.Solver()
        solver.add(domain.charms[0].exists)
        add_constraints(solver, domain)
        assert solver.check() == z3.sat
        model = solver.model()

        # WHEN extracting
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN the application name falls back to the charm name
        assert "grafana-agent-k8s" in solution.bundles[0].applications

    def test_duplicate_charm_names_get_suffixes(self) -> None:
        # GIVEN a domain with two instances of the same charm (no app constraint match)
        from bundle_builder_x.domain import add_charm_to_domain

        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                )
            }
        )
        charm_a = _make_charm("my-charm", revision=1)
        charm_b = _make_charm("my-charm", revision=2)
        add_charm_to_domain(charm_a, domain, ModelRef(name="m"))
        add_charm_to_domain(charm_b, domain, ModelRef(name="m"))

        # Force both to exist
        solver = z3.Solver()
        solver.add(domain.charms[0].exists)
        solver.add(domain.charms[1].exists)
        add_constraints(solver, domain)
        assert solver.check() == z3.sat
        model = solver.model()

        # WHEN extracting
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN both appear with distinct names
        app_names = set(solution.bundles[0].applications.keys())
        assert len(app_names) == 2
        assert "my-charm" in app_names  # first one gets the base name
        assert any(n.startswith("my-charm-") for n in app_names)  # second gets a suffix

    def test_local_integration_appears_in_bundle(self) -> None:
        # GIVEN two charms with a local integration
        from bundle_builder_x.domain import add_charm_to_domain

        provider = _make_charm(
            "postgresql-k8s",
            endpoints={"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql", optional=True)},
        )
        requirer = _make_charm(
            "app",
            endpoints={"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql", optional=True)},
        )
        apps = {
            "pg": DomainApplication(charm="postgresql-k8s"),
            "app": DomainApplication(charm="app"),
        }
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications=apps,
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="app", endpoint="db"),
                            endpoint_2=DomainApplicationEndpoint(application="pg", endpoint="database"),
                        )
                    ],
                )
            }
        )
        add_charm_to_domain(provider, domain, ModelRef(name="m"))
        add_charm_to_domain(requirer, domain, ModelRef(name="m"))

        # WHEN extracting
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN the bundle has one integration
        bundle = solution.bundles[0]
        assert len(bundle.integrations) == 1
        integration = next(iter(bundle.integrations))
        endpoints = {f"{ep.application}:{ep.endpoint}" for ep in integration}
        assert endpoints == {"pg:database", "app:db"}

    def test_user_cmr_appears_in_bundle(self) -> None:
        # GIVEN a domain with a user-specified cross-model integration
        from bundle_builder_x.domain import add_charm_to_domain

        provider = _make_charm(
            "postgresql-k8s",
            endpoints={"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql", optional=True)},
        )
        requirer = _make_charm(
            "app",
            endpoints={"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql", optional=True)},
        )
        cmr_integration = DomainApplicationIntegration(
            endpoint_1=DomainApplicationEndpoint(application="app", endpoint="db"),
            endpoint_2=DomainApplicationEndpoint(
                application="pg", endpoint="database", model=ModelRef(name="provider-model")
            ),
            offer_name="pg-offer",
            url="lxd:admin/provider-model.pg-offer",
        )
        domain = _make_domain(
            {
                ModelRef(name="provider-model"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"pg": DomainApplication(charm="postgresql-k8s")},
                    controller="lxd",
                ),
                ModelRef(name="consumer-model"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"app": DomainApplication(charm="app")},
                    application_integrations=[cmr_integration],
                ),
            }
        )
        add_charm_to_domain(provider, domain, ModelRef(name="provider-model"))
        add_charm_to_domain(requirer, domain, ModelRef(name="consumer-model"))

        # WHEN extracting
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN the consumer bundle has a requires-side CMR
        consumer_bundle = next(b for b in solution.bundles if b.model == "consumer-model")
        assert len(consumer_bundle.cross_model_integrations) == 1
        cmr = consumer_bundle.cross_model_integrations[0]
        assert cmr.local.application == "app"
        assert cmr.local.endpoint == "db"
        assert cmr.remote_model == "provider-model"
        assert cmr.url == "lxd:admin/provider-model.pg-offer"

        # AND the provider bundle gets a mirrored provides-side CMR
        provider_bundle = next(b for b in solution.bundles if b.model == "provider-model")
        provides_cmrs = [c for c in provider_bundle.cross_model_integrations if c.local_role == EndpointType.PROVIDES]
        assert len(provides_cmrs) == 1
        assert provides_cmrs[0].local.application == "pg"

    def test_user_cmr_defined_on_provider_side_mirrors_to_consumer(self) -> None:
        # GIVEN a domain where the CMR is defined in the providing model's spec
        # (the providing model lists the remote requiring endpoint, not the other way around)
        from bundle_builder_x.domain import add_charm_to_domain

        provider = _make_charm(
            "prometheus-k8s",
            endpoints={
                "self-metrics-endpoint": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="prometheus_scrape", optional=True
                ),
            },
        )
        requirer = _make_charm(
            "prometheus-k8s",
            endpoints={
                "metrics-endpoint": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="prometheus_scrape", optional=True
                ),
            },
        )
        # The integration constraint lives in the providing model (target-model),
        # pointing at the requiring model (neighbor-model) as the remote.
        # url=None simulates the common case where no explicit URL was provided in the spec.
        cmr_integration = DomainApplicationIntegration(
            endpoint_1=DomainApplicationEndpoint(application="target", endpoint="self-metrics-endpoint"),
            endpoint_2=DomainApplicationEndpoint(
                application="neighbor",
                endpoint="metrics-endpoint",
                model=ModelRef(name="neighbor-model", controller="neighbor-controller"),
            ),
            offer_name="neighbor-offer",
            url=None,
        )
        domain = _make_domain(
            {
                ModelRef(name="target-model", controller="target-controller"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    ref=ModelRef(name="target-model", controller="target-controller"),
                    applications={"target": DomainApplication(charm="prometheus-k8s")},
                    application_integrations=[cmr_integration],
                ),
                ModelRef(name="neighbor-model", controller="neighbor-controller"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    ref=ModelRef(name="neighbor-model", controller="neighbor-controller"),
                    applications={"neighbor": DomainApplication(charm="prometheus-k8s")},
                ),
            }
        )
        add_charm_to_domain(provider, domain, ModelRef(name="target-model", controller="target-controller"))
        add_charm_to_domain(requirer, domain, ModelRef(name="neighbor-model", controller="neighbor-controller"))

        # WHEN extracting
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN the providing (target) bundle has a PROVIDES CMR
        target_bundle = next(b for b in solution.bundles if "target-model" in (b.model or ""))
        target_cmrs = [c for c in target_bundle.cross_model_integrations if c.local_role == EndpointType.PROVIDES]
        assert len(target_cmrs) == 1
        assert target_cmrs[0].local.application == "target"
        assert target_cmrs[0].local.endpoint == "self-metrics-endpoint"

        # AND the requiring (neighbor) bundle gets a mirrored REQUIRES CMR
        neighbor_bundle = next(b for b in solution.bundles if "neighbor-model" in (b.model or ""))
        requires_cmrs = [c for c in neighbor_bundle.cross_model_integrations if c.local_role == EndpointType.REQUIRES]
        assert len(requires_cmrs) == 1
        cmr = requires_cmrs[0]
        assert cmr.local.application == "neighbor"
        assert cmr.local.endpoint == "metrics-endpoint"
        assert cmr.offer_name == "neighbor-offer"
        # URL must point to the providing model's offer, not the requiring model
        assert cmr.url == "target-controller:admin/target-model.neighbor-offer"

        # AND the neighbor bundle exports a saas section and a CMR relation
        neighbor_yaml = yaml.safe_load(neighbor_bundle.export())
        assert "saas" in neighbor_yaml
        assert "neighbor-offer" in neighbor_yaml["saas"]
        assert neighbor_yaml["saas"]["neighbor-offer"]["url"] == "target-controller:admin/target-model.neighbor-offer"
        assert any(
            "neighbor-offer:self-metrics-endpoint" in r and "neighbor:metrics-endpoint" in r
            for r in neighbor_yaml["relations"]
        )

    def test_config_values_extracted_correctly(self) -> None:
        # GIVEN a charm with a fixed config value
        from bundle_builder_x.domain import add_charm_to_domain

        charm = _make_charm(
            "vault-k8s",
            config_defaults={"common_name": "default-cn"},
        )
        # Override configs to declare a fixed value
        charm = charm.model_copy(update={"configs": {"common_name": ["my-cn"]}})
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"vault": DomainApplication(charm="vault-k8s")},
                )
            }
        )
        add_charm_to_domain(charm, domain, ModelRef(name="m"))

        # WHEN extracting
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN the config is present in the application
        assert solution.bundles[0].applications["vault"].config["common_name"] == "my-cn"

    def test_resource_values_extracted_correctly(self) -> None:
        # GIVEN a charm with a fixed resource value
        from bundle_builder_x.domain import add_charm_to_domain

        charm = _make_charm("temporal-worker-k8s")
        charm = charm.model_copy(
            update={"resources": {"temporal-worker-image": ["ghcr.io/canonical/temporal-worker-test:abc123"]}}
        )
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"worker": DomainApplication(charm="temporal-worker-k8s")},
                )
            }
        )
        add_charm_to_domain(charm, domain, ModelRef(name="m"))

        # WHEN extracting
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN the resource is present in the application
        assert solution.bundles[0].applications["worker"].resources["temporal-worker-image"] == (
            "ghcr.io/canonical/temporal-worker-test:abc123"
        )

    def test_optional_resource_omitted_when_unset(self) -> None:
        # GIVEN a charm with an optional resource (null allowed)
        import z3

        from bundle_builder_x.constraints import add_constraints
        from bundle_builder_x.domain import add_charm_to_domain

        charm = _make_charm("temporal-worker-k8s")
        charm = charm.model_copy(
            update={"resources": {"temporal-worker-image": ["ghcr.io/canonical/temporal-worker-test:abc123", None]}}
        )
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"worker": DomainApplication(charm="temporal-worker-k8s")},
                )
            }
        )
        add_charm_to_domain(charm, domain, ModelRef(name="m"))

        # Force the solver to choose the unset option by constraining isset_var to False
        solver = z3.Solver()
        add_constraints(solver, domain)
        res = domain.charms[0].resources["temporal-worker-image"]
        solver.add(res.isset_var == False)  # noqa: E712
        assert solver.check() == z3.sat
        model = solver.model()

        # WHEN extracting
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN the optional resource is absent
        assert "temporal-worker-image" not in solution.bundles[0].applications["worker"].resources

    def test_multi_value_resource_constrained_to_allowed_set(self) -> None:
        # GIVEN a charm with two allowed resource values
        from bundle_builder_x.domain import add_charm_to_domain

        allowed = ["ghcr.io/foo:v1", "ghcr.io/foo:v2"]
        charm = _make_charm("temporal-worker-k8s")
        charm = charm.model_copy(update={"resources": {"temporal-worker-image": allowed}})
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"worker": DomainApplication(charm="temporal-worker-k8s")},
                )
            }
        )
        add_charm_to_domain(charm, domain, ModelRef(name="m"))

        # WHEN solving and extracting
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN the extracted value is one of the declared allowed values (not an arbitrary string)
        result = solution.bundles[0].applications["worker"].resources["temporal-worker-image"]
        assert result in allowed

    def test_optional_resource_value_constrained_to_allowed_set_when_set(self) -> None:
        # GIVEN a charm with an optional resource with two allowed non-None values
        import z3

        from bundle_builder_x.constraints import add_constraints
        from bundle_builder_x.domain import add_charm_to_domain

        allowed_values = ["ghcr.io/foo:v1", "ghcr.io/foo:v2"]
        charm = _make_charm("temporal-worker-k8s")
        charm = charm.model_copy(update={"resources": {"temporal-worker-image": [*allowed_values, None]}})
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"worker": DomainApplication(charm="temporal-worker-k8s")},
                )
            }
        )
        add_charm_to_domain(charm, domain, ModelRef(name="m"))

        # Force the resource to be set
        solver = z3.Solver()
        add_constraints(solver, domain)
        res = domain.charms[0].resources["temporal-worker-image"]
        solver.add(res.isset_var == True)  # noqa: E712
        assert solver.check() == z3.sat
        model = solver.model()

        # WHEN extracting
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN the extracted value is one of the declared allowed values
        result = solution.bundles[0].applications["worker"].resources["temporal-worker-image"]
        assert result in allowed_values


class TestExtractMultiModel:
    def test_bundles_per_model(self) -> None:
        # GIVEN a two-model domain with one app each
        from bundle_builder_x.domain import add_charm_to_domain

        charm_a = _make_charm("charm-a")
        charm_b = _make_charm("charm-b")
        domain = _make_domain(
            {
                ModelRef(name="model-a"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"app-a": DomainApplication(charm="charm-a")},
                ),
                ModelRef(name="model-b"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"app-b": DomainApplication(charm="charm-b")},
                ),
            }
        )
        add_charm_to_domain(charm_a, domain, ModelRef(name="model-a"))
        add_charm_to_domain(charm_b, domain, ModelRef(name="model-b"))

        # WHEN extracting
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN we get two bundles
        assert len(solution.bundles) == 2
        model_names = {b.model for b in solution.bundles}
        assert model_names == {"model-a", "model-b"}
