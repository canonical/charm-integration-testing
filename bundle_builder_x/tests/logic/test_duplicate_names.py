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

"""Tests for solver correctness when the same name appears in multiple models.

Covers:
- Same application name in two different models (each resolved independently).
- Same application name in two different models where one requires expansion.
- Same application name in two different models where only one can expand.
"""

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.domain import ApplicationConstraint, ModelInit, initialize_global_domain
from bundle_builder_x.extract import extract_solution

from .conftest import JUJU, CharmhubClientStub, make_charm


class TestSameApplicationNameAcrossModels:
    def test_same_app_name_resolved_independently_per_model(self) -> None:
        # GIVEN two charms that share a name ("db") but belong to different models
        # with different platforms, and a stub that serves each correctly
        k8s_db = make_charm("postgresql-k8s", channel="14/stable")
        machine_db = make_charm("postgresql", channel="14/stable")

        stub = CharmhubClientStub(k8s_db, machine_db)
        builder = BundleBuilder(charmhub_client=stub)

        # WHEN two models both declare an application called "db" with different charms
        domain = initialize_global_domain(
            {
                "k8s-model": ModelInit(
                    applications={"db": ApplicationConstraint(charm="postgresql-k8s")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=JUJU,
                ),
                "machine-model": ModelInit(
                    applications={"db": ApplicationConstraint(charm="postgresql")},
                    platform="machine",
                    arch="amd64",
                    juju_version=JUJU,
                ),
            }
        )
        z3_model = builder._solve(domain)
        solution = extract_solution(z3_model, domain, builder.logger)
        k8s_bundle = next(b for b in solution.bundles if b.model == "k8s-model")
        machine_bundle = next(b for b in solution.bundles if b.model == "machine-model")

        # THEN each model resolves its own "db" to the correct charm
        assert k8s_bundle.applications["db"].charm.name == "postgresql-k8s"
        assert machine_bundle.applications["db"].charm.name == "postgresql"

    def test_same_app_name_expansion_in_one_model_does_not_affect_other(self) -> None:
        # GIVEN "app" in k8s-model has a required endpoint that needs expansion,
        # while "app" in machine-model is self-contained
        needs_db = make_charm(
            "webapp",
            channel="stable",
            endpoints={
                "db": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="postgresql",
                    optional=False,
                ),
            },
        )
        provider = make_charm(
            "postgresql-k8s",
            channel="stable",
            endpoints={
                "database": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="postgresql",
                    optional=True,
                ),
            },
        )
        standalone = make_charm("standalone-app", channel="stable")

        stub = CharmhubClientStub(needs_db, provider, standalone)
        builder = BundleBuilder(charmhub_client=stub)

        domain = initialize_global_domain(
            {
                "k8s-model": ModelInit(
                    applications={"app": ApplicationConstraint(charm="webapp")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=JUJU,
                ),
                "machine-model": ModelInit(
                    applications={"app": ApplicationConstraint(charm="standalone-app")},
                    platform="machine",
                    arch="amd64",
                    juju_version=JUJU,
                ),
            }
        )
        z3_model = builder._solve(domain)
        solution = extract_solution(z3_model, domain, builder.logger)
        k8s_bundle = next(b for b in solution.bundles if b.model == "k8s-model")
        machine_bundle = next(b for b in solution.bundles if b.model == "machine-model")

        # THEN k8s-model expanded to include a postgresql-k8s for "app"'s db requirement
        k8s_charm_names = {a.charm.name for a in k8s_bundle.applications.values()}
        assert "webapp" in k8s_charm_names
        assert "postgresql-k8s" in k8s_charm_names

        # AND machine-model's "app" was resolved without any expansion
        assert len(machine_bundle.applications) == 1
        assert machine_bundle.applications["app"].charm.name == "standalone-app"

    def test_same_app_name_unsatisfiable_in_one_model_raises(self) -> None:
        # GIVEN "app" in k8s-model has a required endpoint with no available provider,
        # while "app" in machine-model is fine - the whole build should fail
        needs_db = make_charm(
            "webapp",
            channel="stable",
            endpoints={
                "db": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="no-such-interface",
                    optional=False,
                ),
            },
        )
        standalone = make_charm("standalone-app", channel="stable")

        stub = CharmhubClientStub(needs_db, standalone)
        builder = BundleBuilder(charmhub_client=stub)

        domain = initialize_global_domain(
            {
                "k8s-model": ModelInit(
                    applications={"app": ApplicationConstraint(charm="webapp")},
                    platform="kubernetes",
                    arch="amd64",
                    juju_version=JUJU,
                ),
                "machine-model": ModelInit(
                    applications={"app": ApplicationConstraint(charm="standalone-app")},
                    platform="machine",
                    arch="amd64",
                    juju_version=JUJU,
                ),
            }
        )

        # THEN the solver fails because k8s-model cannot be completed
        with pytest.raises(UncompletableBundleError):
            builder._solve(domain)
