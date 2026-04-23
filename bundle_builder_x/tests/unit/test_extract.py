# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Unit tests for extract.py: extracting a Solution from a Z3 model."""

import logging

import z3  # type: ignore[import-untyped]

from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.constraints import add_constraints
from bundle_builder_x.domain import (
    ApplicationConstraint,
    CrossModelIntegrationConstraint,
    CrossModelRemote,
    IntegrationConstraint,
    ModelInit,
    initialize_global_domain,
)
from bundle_builder_x.extract import extract_solution
from bundle_builder_x.juju_version import JujuVersion

_JUJU = JujuVersion(major=3, minor=6, patch=0)
_LOGGER = logging.getLogger("test_extract")


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

        domain = initialize_global_domain(
            {
                "m": ModelInit(
                    applications={"my-pg": ApplicationConstraint(charm="postgresql-k8s")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                )
            }
        )
        charm = _make_charm(
            "postgresql-k8s",
            endpoints={"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql", optional=True)},
        )
        add_charm_to_domain(charm, domain, "m")

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

        domain = initialize_global_domain(
            {
                "m": ModelInit(
                    applications={},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                )
            }
        )
        charm = _make_charm("grafana-agent-k8s")
        add_charm_to_domain(charm, domain, "m")
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

        domain = initialize_global_domain(
            {
                "m": ModelInit(
                    applications={},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                )
            }
        )
        charm_a = _make_charm("my-charm", revision=1)
        charm_b = _make_charm("my-charm", revision=2)
        add_charm_to_domain(charm_a, domain, "m")
        add_charm_to_domain(charm_b, domain, "m")

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
        domain = initialize_global_domain(
            {
                "m": ModelInit(
                    applications={
                        "pg": ApplicationConstraint(charm="postgresql-k8s"),
                        "app": ApplicationConstraint(charm="app"),
                    },
                    integrations={
                        IntegrationConstraint(
                            application_1="pg", endpoint_1="database", application_2="app", endpoint_2="db"
                        )
                    },
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                )
            }
        )
        add_charm_to_domain(provider, domain, "m")
        add_charm_to_domain(requirer, domain, "m")

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
        domain = initialize_global_domain(
            {
                "provider-model": ModelInit(
                    applications={"pg": ApplicationConstraint(charm="postgresql-k8s")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                    controller="lxd",
                ),
                "consumer-model": ModelInit(
                    applications={"app": ApplicationConstraint(charm="app")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                    cross_model_integrations=[
                        CrossModelIntegrationConstraint(
                            local_application="app",
                            local_endpoint="db",
                            remote=CrossModelRemote(
                                model="provider-model",
                                application="pg",
                                endpoint="database",
                                offer_name="pg-offer",
                                url="lxd:admin/provider-model.pg-offer",
                            ),
                        )
                    ],
                ),
            }
        )
        add_charm_to_domain(provider, domain, "provider-model")
        add_charm_to_domain(requirer, domain, "consumer-model")

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

    def test_config_values_extracted_correctly(self) -> None:
        # GIVEN a charm with a fixed config value
        from bundle_builder_x.domain import add_charm_to_domain

        charm = _make_charm(
            "vault-k8s",
            config_defaults={"common_name": "default-cn"},
        )
        # Override configs to declare a fixed value
        charm = charm.model_copy(update={"configs": {"common_name": ["my-cn"]}})
        domain = initialize_global_domain(
            {
                "m": ModelInit(
                    applications={"vault": ApplicationConstraint(charm="vault-k8s")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                )
            }
        )
        add_charm_to_domain(charm, domain, "m")

        # WHEN extracting
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN the config is present in the application
        assert solution.bundles[0].applications["vault"].config["common_name"] == "my-cn"


class TestExtractMultiModel:
    def test_bundles_per_model(self) -> None:
        # GIVEN a two-model domain with one app each
        from bundle_builder_x.domain import add_charm_to_domain

        charm_a = _make_charm("charm-a")
        charm_b = _make_charm("charm-b")
        domain = initialize_global_domain(
            {
                "model-a": ModelInit(
                    applications={"app-a": ApplicationConstraint(charm="charm-a")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                ),
                "model-b": ModelInit(
                    applications={"app-b": ApplicationConstraint(charm="charm-b")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                ),
            }
        )
        add_charm_to_domain(charm_a, domain, "model-a")
        add_charm_to_domain(charm_b, domain, "model-b")

        # WHEN extracting
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)

        # THEN we get two bundles
        assert len(solution.bundles) == 2
        model_names = {b.model for b in solution.bundles}
        assert model_names == {"model-a", "model-b"}
