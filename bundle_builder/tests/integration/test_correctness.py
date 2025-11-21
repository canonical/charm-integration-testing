# Copyright (C) 2025 Canonical Ltd

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


from bundle_builder import Application, ApplicationEndpoint, Bundle, BundleBuilder, CharmhubClient, Integration


def test_correctness_independent(
    charmhub_client: CharmhubClient,
    sample_independent_charm: str,
    sample_independent_charm_revision: int,
    sample_arch: str,
    sample_platform: str,
):
    # GIVEN a base bundle with a dependent charm
    base_bundle = Bundle(
        applications=frozenset(
            {
                Application(
                    sample_independent_charm,
                    charmhub_client.charm_from_store(
                        charm_name=sample_independent_charm,
                        charm_revision=sample_independent_charm_revision,
                        ubuntu_arch=sample_arch,
                    ),
                )
            }
        ),
        integrations=frozenset(),
        platform=sample_platform,
        arch=sample_arch,
    )

    # WHEN minimal bundle is built
    minimal_bundle = BundleBuilder(charmhub_client).build(base_bundle)

    # THEN the minimal bundle contains only the independent charm
    assert {application.name for application in minimal_bundle.applications} == {sample_independent_charm}
    # AND the minimal bundle contains no integrations
    assert len(minimal_bundle.integrations) == 0


def test_correctness_dependent(
    charmhub_client: CharmhubClient,
    sample_independent_charm: str,
    sample_dependent_charm: str,
    sample_independent_charm_endpoint: str,
    sample_dependent_charm_endpoint: str,
    sample_dependent_charm_revision: int,
    sample_arch: str,
    sample_platform: str,
):
    # GIVEN a base bundle of the independent charm
    base_bundle = Bundle(
        applications=frozenset(
            {
                Application(
                    sample_dependent_charm,
                    charmhub_client.charm_from_store(
                        charm_name=sample_dependent_charm,
                        charm_revision=sample_dependent_charm_revision,
                        ubuntu_arch=sample_arch,
                    ),
                )
            },
        ),
        integrations=frozenset(),
        platform=sample_platform,
        arch=sample_arch,
    )

    # WHEN minimal bundle is built
    minimal_bundle = BundleBuilder(charmhub_client).build(base_bundle)

    # THEN the minimal bundle contains both the dependent and independent charm
    assert {application.name for application in minimal_bundle.applications} == {
        sample_dependent_charm,
        sample_independent_charm,
    }
    # AND the minimal bundle contains the integration between them
    assert minimal_bundle.integrations == {
        Integration(
            {
                ApplicationEndpoint(application=sample_dependent_charm, endpoint=sample_dependent_charm_endpoint),
                ApplicationEndpoint(application=sample_independent_charm, endpoint=sample_independent_charm_endpoint),
            }
        )
    }
