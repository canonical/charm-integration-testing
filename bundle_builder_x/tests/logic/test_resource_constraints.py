# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Logic tests for juju_constraint[key] resource constraints.

Covers the `juju_constraint[cores|mem|root-disk] >= N` DSL constraints which
force the bundle builder to set Juju resource constraints on a charm.  The
optimizer minimises resource consumption, so the solved value equals the
smallest N satisfying all constraints.

On machine clouds the exported value is a minimum; on Kubernetes it is a cap.
In both cases the solver produces the tightest-fitting value.
"""

from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.spec import AppSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm


class TestCoresConstraint:
    """juju_constraint[cores] >= N forces the solver to emit exactly N cores."""

    def test_cores_ge_2_emits_cores_2(self) -> None:
        # GIVEN a charm with a minimum 2-core constraint
        charm = make_charm(
            "my-charm",
            constraint_strs=["juju_constraint[cores] >= 2"],
        )
        stub = CharmhubClientStub(charm)
        builder = BundleBuilder(charmhub_client=stub)

        # WHEN building a single-model bundle
        bundle = build_single_model(builder, {"my-charm": AppSpec(charm="my-charm")})

        # THEN the application has exactly 2 cores (optimizer minimizes)
        app = bundle.applications.get("my-charm")
        assert app is not None
        assert app.juju_constraints.get("cores") == 2

    def test_no_cores_constraint_uses_default(self) -> None:
        # GIVEN a charm with no resource constraints
        charm = make_charm("my-charm")
        stub = CharmhubClientStub(charm)
        builder = BundleBuilder(charmhub_client=stub)

        # WHEN building a single-model bundle
        bundle = build_single_model(builder, {"my-charm": AppSpec(charm="my-charm")})

        # THEN juju_constraints contains the default value for cores
        app = bundle.applications.get("my-charm")
        assert app is not None
        assert app.juju_constraints.get("cores") == 1


class TestMemConstraint:
    """juju_constraint[mem] >= N forces the solver to emit exactly N MB."""

    def test_mem_ge_4096_emits_mem_4096(self) -> None:
        # GIVEN a charm requiring at least 4096 MB RAM
        charm = make_charm(
            "my-charm",
            constraint_strs=["juju_constraint[mem] >= 4096"],
        )
        stub = CharmhubClientStub(charm)
        builder = BundleBuilder(charmhub_client=stub)

        # WHEN building a single-model bundle
        bundle = build_single_model(builder, {"my-charm": AppSpec(charm="my-charm")})

        # THEN mem is exactly 4096 MB
        app = bundle.applications.get("my-charm")
        assert app is not None
        assert app.juju_constraints.get("mem") == 4096


class TestRootDiskConstraint:
    """juju_constraint[root-disk] >= N forces the solver to emit exactly N MB."""

    def test_root_disk_ge_20480_emits_root_disk_20480(self) -> None:
        # GIVEN a charm requiring at least 20 GB root disk
        charm = make_charm(
            "my-charm",
            constraint_strs=["juju_constraint[root-disk] >= 20480"],
        )
        stub = CharmhubClientStub(charm)
        builder = BundleBuilder(charmhub_client=stub)

        # WHEN building a single-model bundle
        bundle = build_single_model(builder, {"my-charm": AppSpec(charm="my-charm")})

        # THEN root-disk is exactly 20480 MB
        app = bundle.applications.get("my-charm")
        assert app is not None
        assert app.juju_constraints.get("root-disk") == 20480


class TestCombinedResourceConstraints:
    """Multiple resource dimensions can be constrained independently."""

    def test_all_three_dimensions_constrained(self) -> None:
        # GIVEN a charm with cores, mem, and root-disk constraints
        charm = make_charm(
            "my-charm",
            constraint_strs=[
                "juju_constraint[cores] >= 2",
                "juju_constraint[mem] >= 2048",
                "juju_constraint[root-disk] >= 10240",
            ],
        )
        stub = CharmhubClientStub(charm)
        builder = BundleBuilder(charmhub_client=stub)

        # WHEN building a single-model bundle
        bundle = build_single_model(builder, {"my-charm": AppSpec(charm="my-charm")})

        # THEN all three dimensions are set to their exact minimum values
        app = bundle.applications.get("my-charm")
        assert app is not None
        assert app.juju_constraints == {"cores": 2, "mem": 2048, "root-disk": 10240}
