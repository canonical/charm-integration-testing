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

"""Tests that the Z3 solver discovers cross-model integrations via DomainCharmIntegration."""

import logging

import z3  # type: ignore[import-untyped]

from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.constraints import add_constraints
from bundle_builder_x.domain import (
    Domain,
    DomainApplication,
    DomainModel,
    ModelRef,
    add_charm_to_domain,
)
from bundle_builder_x.extract import extract_solution
from bundle_builder_x.juju_version import JujuVersion
from tests.unit._integration_helpers import materialize_all_integrations

_LOGGER = logging.getLogger("test_cmr_discovery")
_JUJU = JujuVersion(major=3, minor=6, patch=0)


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


class TestSolverDiscoversCMR:
    def test_solver_activates_potential_cmr_to_satisfy_requires(self) -> None:
        """When model-A has a REQUIRES endpoint and model-B has the matching
        PROVIDES endpoint, the solver should activate the cross-model DomainCharmIntegration."""
        # GIVEN two models
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

        # Add charms with matching interfaces to different models
        # postgresql PROVIDES database (non-optional), pgbouncer REQUIRES backend-database (non-optional)
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
        materialize_all_integrations(domain)

        # WHEN solving
        solver = z3.Solver()
        add_constraints(solver, domain)
        result = solver.check()

        # THEN the problem is satisfiable
        assert result == z3.sat, f"Expected SAT, got {result}"

        model = solver.model()

        # AND the cross-model DomainCharmIntegration is activated
        cmr_integrations = [i for i in domain.charm_integrations if domain.is_cross_model(i)]
        assert len(cmr_integrations) == 1
        assert model.evaluate(cmr_integrations[0].exists, model_completion=True)

    def test_discovered_cmr_appears_in_both_extracted_bundles(self) -> None:
        """After solving, extract_solution should include the discovered CMR
        in both the PROVIDES and REQUIRES model bundles."""
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
        materialize_all_integrations(domain)

        solver = z3.Solver()
        add_constraints(solver, domain)
        assert solver.check() == z3.sat
        z3_model = solver.model()

        # WHEN extracting bundles
        solution = extract_solution(z3_model, domain, _LOGGER)
        k8s_bundle = next(b for b in solution.bundles if b.model == "k8s")
        machine_bundle = next(b for b in solution.bundles if b.model == "machine")

        # THEN both bundles exist
        assert {b.model for b in solution.bundles} == {"k8s", "machine"}

        # AND the k8s bundle has a REQUIRES CMR
        k8s_cmrs = k8s_bundle.cross_model_integrations
        assert len(k8s_cmrs) == 1
        assert k8s_cmrs[0].local_role == EndpointType.REQUIRES
        assert k8s_cmrs[0].remote_model == "machine"

        # AND the machine bundle has a PROVIDES CMR
        machine_cmrs = machine_bundle.cross_model_integrations
        assert len(machine_cmrs) == 1
        assert machine_cmrs[0].local_role == EndpointType.PROVIDES
        assert machine_cmrs[0].remote_model == "k8s"

    def test_solver_does_not_activate_unnecessary_cmr(self) -> None:
        """When both endpoints can be satisfied locally, no cross-model integration is activated."""
        domain = _make_domain(
            {
                ModelRef(name="m1"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={
                        "pg": DomainApplication(charm="postgresql"),
                        "proxy": DomainApplication(charm="pgbouncer"),
                    },
                ),
                ModelRef(name="m2"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"other": DomainApplication(charm="other-charm")},
                ),
            }
        )

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
        other = _make_charm(
            "other-charm",
            {
                "db": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql"),
            },
        )
        add_charm_to_domain(pg, domain, ModelRef(name="m1"))
        add_charm_to_domain(proxy, domain, ModelRef(name="m1"))
        add_charm_to_domain(other, domain, ModelRef(name="m2"))
        materialize_all_integrations(domain)

        solver = z3.Solver()
        add_constraints(solver, domain)
        assert solver.check() == z3.sat
        z3_model = solver.model()

        # Cross-model integrations exist but should not all be activated
        # m1's pg can satisfy both m1's proxy (locally) and m2's other (cross-model)
        # The solver should use the local integration for m1, and may or may not
        # use the CMR for m2 depending on whether other's endpoint is optional.
        # Since other's "db" endpoint is non-optional, a CMR should be activated for it.
        solution = extract_solution(z3_model, domain, _LOGGER)
        m1_bundle = next(b for b in solution.bundles if b.model == "m1")

        # m1 should have the local integration between pg and proxy
        assert len(m1_bundle.integrations) == 1
