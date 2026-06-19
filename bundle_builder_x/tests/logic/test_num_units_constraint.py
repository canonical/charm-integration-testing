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

"""Logic tests for count({self}) unit-count constraints.

Covers the `count(charm_set) >= N` DSL constraint which forces the bundle
builder to deploy an application with at least N units.

Real example: OpenSearch requires a minimum of 3 units for HA.
"""

from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.spec import AppSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm


class TestCountSelfConstraint:
    """count({self}) forces the solver to emit a unit count >= N."""

    def test_count_self_ge_3_emits_scale_3(self) -> None:
        # GIVEN a charm with constraint count({self}) >= 3
        opensearch = make_charm(
            "opensearch",
            constraint_strs=["len(units({self})) >= 3"],
        )
        stub = CharmhubClientStub(opensearch)
        builder = BundleBuilder(charmhub_client=stub)

        # WHEN building a single-model bundle
        bundle = build_single_model(builder, {"opensearch": AppSpec(charm="opensearch")})

        # THEN the exported application has scale >= 3
        app = bundle.applications.get("opensearch")
        assert app is not None
        assert app.num_units >= 3

    def test_no_count_constraint_defaults_to_1(self) -> None:
        # GIVEN a charm with no unit-count constraint
        charm = make_charm("my-charm")
        stub = CharmhubClientStub(charm)
        builder = BundleBuilder(charmhub_client=stub)

        # WHEN building a single-model bundle
        bundle = build_single_model(builder, {"my-charm": AppSpec(charm="my-charm")})

        # THEN the exported application has scale 1 (the minimum default)
        app = bundle.applications.get("my-charm")
        assert app is not None
        assert app.num_units == 1
