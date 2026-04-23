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

"""Tests for global multi-model domain behavior."""

from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.domain import (
    ApplicationConstraint,
    ModelInit,
    add_charm_to_domain,
    initialize_global_domain,
)
from bundle_builder_x.juju_version import JujuVersion


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
        domain = initialize_global_domain(
            {
                "k8s": ModelInit(
                    applications={"app-a": ApplicationConstraint(charm="charm-a")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                ),
                "machine": ModelInit(
                    applications={"app-b": ApplicationConstraint(charm="charm-b")},
                    platform="machine",
                    arch="amd64",
                    juju_version=_JUJU,
                ),
            }
        )

        # THEN both models are in the domain
        assert set(domain.models) == {"k8s", "machine"}
        assert domain.models["k8s"].platform == "kubernetes"
        assert domain.models["machine"].platform == "machine"

    def test_global_charms_list_starts_empty(self) -> None:
        domain = initialize_global_domain(
            {
                "m": ModelInit(applications={}, platform="kubernetes", arch="amd64", juju_version=_JUJU),
            }
        )
        assert len(domain.charms) == 0


class TestAddCharmCrossModelPairing:
    def test_same_model_creates_local_integration(self) -> None:
        # GIVEN a domain with one model
        domain = initialize_global_domain(
            {
                "m1": ModelInit(
                    applications={"pg": ApplicationConstraint(charm="postgresql")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
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
        add_charm_to_domain(pg, domain, "m1")
        add_charm_to_domain(proxy, domain, "m1")

        # THEN a local CharmIntegration is created (no PotentialCMR)
        assert len(domain.charm_integrations) == 1
        assert len(domain.potential_cmrs) == 0

    def test_cross_model_creates_potential_cmr(self) -> None:
        # GIVEN a domain with two models
        domain = initialize_global_domain(
            {
                "k8s": ModelInit(
                    applications={"proxy": ApplicationConstraint(charm="pgbouncer")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                ),
                "machine": ModelInit(
                    applications={"pg": ApplicationConstraint(charm="postgresql")},
                    platform="machine",
                    arch="amd64",
                    juju_version=_JUJU,
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
        add_charm_to_domain(pg, domain, "machine")
        add_charm_to_domain(proxy, domain, "k8s")

        # THEN no local integration, but a PotentialCMR is created
        assert len(domain.charm_integrations) == 0
        assert len(domain.potential_cmrs) == 1

        pcmr = domain.potential_cmrs[0]
        assert pcmr.provides_model == "machine"
        assert pcmr.requires_model == "k8s"
        assert pcmr.interface == "postgresql"

    def test_no_potential_cmr_for_mismatched_interfaces(self) -> None:
        # GIVEN two models with charms that have different interfaces
        domain = initialize_global_domain(
            {
                "m1": ModelInit(applications={}, platform="kubernetes", arch="amd64", juju_version=_JUJU),
                "m2": ModelInit(applications={}, platform="machine", arch="amd64", juju_version=_JUJU),
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
        add_charm_to_domain(charm_a, domain, "m1")
        add_charm_to_domain(charm_b, domain, "m2")

        # THEN no PotentialCMR
        assert len(domain.potential_cmrs) == 0

    def test_charm_to_model_tracking(self) -> None:
        # GIVEN a domain with two models
        domain = initialize_global_domain(
            {
                "m1": ModelInit(applications={}, platform="kubernetes", arch="amd64", juju_version=_JUJU),
                "m2": ModelInit(applications={}, platform="machine", arch="amd64", juju_version=_JUJU),
            }
        )

        charm_a = _make_charm("charm-a")
        charm_b = _make_charm("charm-b")
        id_a = add_charm_to_domain(charm_a, domain, "m1")
        id_b = add_charm_to_domain(charm_b, domain, "m2")

        # THEN charm_to_model tracks correctly
        assert domain.charm_to_model[id_a] == "m1"
        assert domain.charm_to_model[id_b] == "m2"

    def test_application_mappings_scoped_to_model(self) -> None:
        # GIVEN a domain with two models, each with a charm named "postgresql"
        domain = initialize_global_domain(
            {
                "m1": ModelInit(
                    applications={"pg-1": ApplicationConstraint(charm="postgresql")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=_JUJU,
                ),
                "m2": ModelInit(
                    applications={"pg-2": ApplicationConstraint(charm="postgresql")},
                    platform="machine",
                    arch="amd64",
                    juju_version=_JUJU,
                ),
            }
        )

        pg = _make_charm(
            "postgresql",
            {
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql"),
            },
        )
        add_charm_to_domain(pg, domain, "m1")
        add_charm_to_domain(pg, domain, "m2")

        # THEN each model has exactly one mapping
        assert len(domain.models["m1"].application_to_charm) == 1
        assert len(domain.models["m2"].application_to_charm) == 1
