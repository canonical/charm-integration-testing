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

"""Logic tests for subordinate charm support.

Covers:
  1. juju-info implicit endpoint injection: only principal (non-subordinate) charms
     implicitly provide juju-info with scope=global. Subordinate charms explicitly
     require it; they do not get the implicit provides injection.
  2. Platform gating: juju-info integrations are only created on machine models
     (subordinates are a machine-only concept in Juju).
  3. Cross-model blocking: juju-info integrations never span models, since
     subordinates must be co-located with their principal.
  4. Base matching: container-scoped integrations require matching ubuntu_version
     between the subordinate and its principal.
  5. Base mismatch expansion: when bases differ, the solver attempts to fetch
     an alternative version of the subordinate (or principal) to resolve it.
  6. Multiple container-scoped endpoints: a subordinate with more than one
     container-scoped endpoint must not cause Z3 tag collisions (each
     integration gets a unique assertion tag keyed by endpoint name).
"""

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import CharmEndpoint, EndpointScope, EndpointType
from bundle_builder_x.spec import AppSpec, ModelSpec

from .conftest import JUJU_VERSION, CharmhubClientStub, build_multi_model, build_single_model, make_charm


class TestSubordinateOnMachine:
    """Subordinate charms integrate via juju-info on machine models."""

    def test_subordinate_integrates_with_principal_on_machine(self) -> None:
        # GIVEN a principal charm and a subordinate charm (requires juju-info with container scope)
        principal = make_charm(
            "ubuntu",
            endpoints={
                "juju-info": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-info", optional=True, scope=EndpointScope.GLOBAL
                ),
            },
        )
        subordinate = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=False, scope=EndpointScope.CONTAINER
                ),
                "monitors": CharmEndpoint(type=EndpointType.PROVIDES, interface="monitors", optional=True),
            },
            subordinate=True,
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(principal, subordinate))

        # WHEN both are pinned in a machine model
        bundle = build_single_model(
            builder,
            applications={
                "ubuntu": AppSpec(charm="ubuntu"),
                "nrpe": AppSpec(charm="nrpe"),
            },
            platform="machine",
        )

        # THEN both are present and integrated
        assert "ubuntu" in bundle.applications
        assert "nrpe" in bundle.applications
        integrations_interfaces = [{ep.endpoint for ep in integration} for integration in bundle.integrations]
        assert any("general-info" in eps and "juju-info" in eps for eps in integrations_interfaces)

    def test_subordinate_auto_discovered_for_principal(self) -> None:
        # GIVEN a principal with a non-optional requires endpoint on "monitors" interface
        # AND a subordinate that provides "monitors" (and requires juju-info)
        principal = make_charm(
            "ubuntu",
            endpoints={
                "juju-info": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-info", optional=True, scope=EndpointScope.GLOBAL
                ),
                "monitors": CharmEndpoint(type=EndpointType.REQUIRES, interface="monitors", optional=False),
            },
        )
        subordinate = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=True, scope=EndpointScope.CONTAINER
                ),
                "monitors": CharmEndpoint(type=EndpointType.PROVIDES, interface="monitors", optional=True),
            },
            subordinate=True,
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(principal, subordinate))

        # WHEN building with only the principal
        bundle = build_single_model(
            builder,
            applications={"ubuntu": AppSpec(charm="ubuntu")},
            platform="machine",
        )

        # THEN the solver discovers and adds the subordinate
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert "nrpe" in charm_names


class TestSubordinateBlockedOnKubernetes:
    """juju-info integrations do not form on Kubernetes models."""

    def test_juju_info_not_integrated_on_k8s(self) -> None:
        # GIVEN a principal and subordinate both in the registry
        principal = make_charm(
            "app",
            endpoints={
                "juju-info": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-info", optional=True, scope=EndpointScope.GLOBAL
                ),
            },
        )
        subordinate = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=False, scope=EndpointScope.CONTAINER
                ),
            },
            subordinate=True,
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(principal, subordinate))

        # WHEN both are pinned on a kubernetes model
        # THEN it fails: the subordinate's non-optional juju-info endpoint cannot be
        # fulfilled because juju-info integrations are blocked on k8s
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={
                    "app": AppSpec(charm="app"),
                    "nrpe": AppSpec(charm="nrpe"),
                },
                platform="kubernetes",
            )

    def test_juju_info_optional_subordinate_allowed_but_unintegrated_on_k8s(self) -> None:
        # GIVEN a subordinate with optional juju-info requirement
        principal = make_charm(
            "app",
            endpoints={
                "juju-info": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-info", optional=True, scope=EndpointScope.GLOBAL
                ),
            },
        )
        subordinate = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=True, scope=EndpointScope.CONTAINER
                ),
            },
            subordinate=True,
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(principal, subordinate))

        # WHEN both pinned on k8s
        bundle = build_single_model(
            builder,
            applications={
                "app": AppSpec(charm="app"),
                "nrpe": AppSpec(charm="nrpe"),
            },
            platform="kubernetes",
        )

        # THEN both exist but there is no juju-info integration
        assert "app" in bundle.applications
        assert "nrpe" in bundle.applications
        # No integrations should reference juju-info
        for integration in bundle.integrations:
            for ep in integration:
                assert ep.endpoint != "general-info"


class TestSubordinateCrossModelBlocked:
    """juju-info integrations never span models (subordinates must be co-located)."""

    def test_cross_model_juju_info_not_created(self) -> None:
        # GIVEN two machine models with a principal in one and a subordinate in the other
        principal = make_charm(
            "ubuntu",
            endpoints={
                "juju-info": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-info", optional=True, scope=EndpointScope.GLOBAL
                ),
            },
        )
        subordinate = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=True, scope=EndpointScope.CONTAINER
                ),
            },
            subordinate=True,
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(principal, subordinate))

        # WHEN principal in model-a, subordinate in model-b
        solution = build_multi_model(
            builder,
            models=[
                ModelSpec(
                    name="model-a",
                    platform="machine",
                    juju=JUJU_VERSION,
                    applications={"ubuntu": AppSpec(charm="ubuntu")},
                ),
                ModelSpec(
                    name="model-b",
                    platform="machine",
                    juju=JUJU_VERSION,
                    applications={"nrpe": AppSpec(charm="nrpe")},
                ),
            ],
        )

        # THEN no cross-model integrations exist at all between the two models
        # (juju-info is blocked cross-model, and since the endpoint is optional the
        # build still succeeds - but no CMR is formed)
        assert all(not bundle.cross_model_integrations for bundle in solution.bundles)


class TestSubordinateBaseMatching:
    """Container-scoped integrations require matching bases (ubuntu_version)."""

    def test_same_base_subordinate_integrates(self) -> None:
        # GIVEN principal and subordinate on the same base (22.04)
        principal = make_charm(
            "ubuntu",
            endpoints={
                "juju-info": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-info", optional=True, scope=EndpointScope.GLOBAL
                ),
            },
            ubuntu_version="22.04",
        )
        subordinate = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=False, scope=EndpointScope.CONTAINER
                ),
            },
            ubuntu_version="22.04",
            subordinate=True,
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(principal, subordinate))

        # WHEN building on machine
        bundle = build_single_model(
            builder,
            applications={
                "ubuntu": AppSpec(charm="ubuntu"),
                "nrpe": AppSpec(charm="nrpe"),
            },
            platform="machine",
        )

        # THEN the integration is formed (same base)
        assert "ubuntu" in bundle.applications
        assert "nrpe" in bundle.applications
        assert len(bundle.integrations) >= 1

    def test_different_base_resolved_by_expansion(self) -> None:
        # GIVEN principal on 22.04 and subordinate initially only on 24.04
        # BUT a 22.04 variant of the subordinate is also available
        principal = make_charm(
            "ubuntu",
            endpoints={
                "juju-info": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-info", optional=True, scope=EndpointScope.GLOBAL
                ),
            },
            ubuntu_version="22.04",
        )
        nrpe_2404 = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=False, scope=EndpointScope.CONTAINER
                ),
            },
            ubuntu_version="24.04",
            subordinate=True,
        )
        # The 22.04 variant that the solver should discover during expansion
        nrpe_2204 = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=False, scope=EndpointScope.CONTAINER
                ),
            },
            ubuntu_version="22.04",
            subordinate=True,
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(principal, nrpe_2404, nrpe_2204))

        # WHEN building (solver hits base mismatch, expands to find the 22.04 variant)
        bundle = build_single_model(
            builder,
            applications={
                "ubuntu": AppSpec(charm="ubuntu"),
                "nrpe": AppSpec(charm="nrpe"),
            },
            platform="machine",
        )

        # THEN both are present and the nrpe charm in the bundle is on 22.04
        assert "ubuntu" in bundle.applications
        assert "nrpe" in bundle.applications
        nrpe_app = bundle.applications["nrpe"]
        assert nrpe_app.charm.ubuntu_version == "22.04"

    def test_different_base_no_variant_raises(self) -> None:
        # GIVEN principal on 22.04 and subordinate only available on 24.04
        principal = make_charm(
            "ubuntu",
            endpoints={
                "juju-info": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-info", optional=True, scope=EndpointScope.GLOBAL
                ),
            },
            ubuntu_version="22.04",
        )
        nrpe_2404 = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=False, scope=EndpointScope.CONTAINER
                ),
            },
            ubuntu_version="24.04",
            subordinate=True,
        )
        # No 22.04 variant available
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(principal, nrpe_2404))

        # WHEN building
        # THEN it fails: base mismatch cannot be resolved
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={
                    "ubuntu": AppSpec(charm="ubuntu"),
                    "nrpe": AppSpec(charm="nrpe"),
                },
                platform="machine",
            )

    def test_global_scope_allows_different_bases(self) -> None:
        # GIVEN two charms with a global-scope integration but different bases
        app = make_charm(
            "app",
            endpoints={
                "database": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="pgsql", optional=False, scope=EndpointScope.GLOBAL
                ),
            },
            ubuntu_version="22.04",
        )
        db = make_charm(
            "database",
            endpoints={
                "database": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="pgsql", optional=True, scope=EndpointScope.GLOBAL
                ),
            },
            ubuntu_version="24.04",
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(app, db))

        # WHEN building on machine
        bundle = build_single_model(
            builder,
            applications={"app": AppSpec(charm="app")},
            platform="machine",
        )

        # THEN the integration forms regardless of base mismatch (global scope has no constraint)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert "database" in charm_names
        assert len(bundle.integrations) >= 1


class TestSubordinateMultipleContainerEndpoints:
    """A subordinate with multiple container-scoped endpoints must not cause Z3 tag collisions."""

    def test_multiple_container_endpoints_same_base(self) -> None:
        # GIVEN a subordinate with two container-scoped endpoints (juju-info + juju-monitoring),
        # both connecting to the same principal. Previously this triggered a Z3
        # "named assertion defined twice" error because both integrations produced
        # the same SubordinateBaseMismatchTag (tag was not keyed per-endpoint).
        principal = make_charm(
            "ubuntu",
            endpoints={
                "juju-info": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-info", optional=True, scope=EndpointScope.GLOBAL
                ),
                "juju-monitoring": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-monitoring", optional=True, scope=EndpointScope.GLOBAL
                ),
            },
            ubuntu_version="22.04",
        )
        subordinate = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=False, scope=EndpointScope.CONTAINER
                ),
                "juju-monitoring": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="juju-monitoring",
                    optional=True,
                    scope=EndpointScope.CONTAINER,
                ),
            },
            ubuntu_version="22.04",
            subordinate=True,
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(principal, subordinate))

        # WHEN building - both container-scoped endpoints are considered; no Z3 crash
        bundle = build_single_model(
            builder,
            applications={
                "ubuntu": AppSpec(charm="ubuntu"),
                "nrpe": AppSpec(charm="nrpe"),
            },
            platform="machine",
        )

        assert "ubuntu" in bundle.applications
        assert "nrpe" in bundle.applications

    def test_multiple_container_endpoints_base_mismatch_resolved(self) -> None:
        # GIVEN a subordinate with two container-scoped endpoints and a base mismatch.
        # Each mismatched integration gets its own unique SubordinateBaseMismatchTag
        # (keyed by endpoint name), so assert_and_track does not collide.
        principal = make_charm(
            "ubuntu",
            endpoints={
                "juju-info": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-info", optional=True, scope=EndpointScope.GLOBAL
                ),
                "juju-monitoring": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-monitoring", optional=True, scope=EndpointScope.GLOBAL
                ),
            },
            ubuntu_version="22.04",
        )
        nrpe_2404 = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=False, scope=EndpointScope.CONTAINER
                ),
                "juju-monitoring": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="juju-monitoring",
                    optional=True,
                    scope=EndpointScope.CONTAINER,
                ),
            },
            ubuntu_version="24.04",
            subordinate=True,
        )
        nrpe_2204 = make_charm(
            "nrpe",
            endpoints={
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", optional=False, scope=EndpointScope.CONTAINER
                ),
                "juju-monitoring": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="juju-monitoring",
                    optional=True,
                    scope=EndpointScope.CONTAINER,
                ),
            },
            ubuntu_version="22.04",
            subordinate=True,
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(principal, nrpe_2404, nrpe_2204))

        # WHEN building - expansion resolves the mismatch without a Z3 tag collision
        bundle = build_single_model(
            builder,
            applications={
                "ubuntu": AppSpec(charm="ubuntu"),
                "nrpe": AppSpec(charm="nrpe"),
            },
            platform="machine",
        )

        assert "nrpe" in bundle.applications
        assert bundle.applications["nrpe"].charm.ubuntu_version == "22.04"
