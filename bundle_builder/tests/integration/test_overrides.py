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


from pathlib import Path

import yaml

from bundle_builder import Application, ApplicationEndpoint, Bundle, BundleBuilder, CharmhubClient, OverridesClient


def test_overrides_metadata_make_optional(
    tmp_path: Path, sample_dependent_charm: str, sample_dependent_charm_endpoint: str
):
    # GIVEN an override to make an endpoint optional
    with (tmp_path / f"{sample_dependent_charm}.yaml").open("w") as f:
        yaml.dump(
            {
                "requires": {
                    sample_dependent_charm_endpoint: {
                        "optional_if": [],
                    }
                }
            },
            f,
        )
    # AND a charmhub client with an overrides client pointed to it
    charmhub_client = CharmhubClient(overrides_client=OverridesClient(charm_metadata_overrides=tmp_path))

    # WHEN a bundle is built with that charm
    minimal_bundle = BundleBuilder(charmhub_client).build(
        Bundle(
            applications=frozenset(
                {
                    Application(
                        name=sample_dependent_charm,
                        charm=charmhub_client.charm_from_store(sample_dependent_charm, "amd64"),
                    )
                }
            ),
            integrations=frozenset(),
            platform="kubernetes",
            arch="amd64",
        )
    )

    # THEN the endpoint is not integrated with in the minimal bundle
    assert ApplicationEndpoint(application=sample_dependent_charm, endpoint=sample_dependent_charm_endpoint) not in {
        application_endpoint for integration in minimal_bundle.integrations for application_endpoint in integration
    }


def test_charm_config(tmp_path: Path, sample_independent_charm: str):
    # GIVEN a charm test config
    with (tmp_path / f"{sample_independent_charm}.yaml").open("w") as f:
        yaml.dump(
            {"configs": [{"config-option": "config-value"}]},
            f,
        )
    # AND a charmhub client with an overrides client pointed to it
    charmhub_client = CharmhubClient(overrides_client=OverridesClient(charm_test_configs=tmp_path))

    # WHEN a bundle is built with that charm
    minimal_bundle = BundleBuilder(charmhub_client).build(
        Bundle(
            applications=frozenset(
                {
                    Application(
                        name=sample_independent_charm,
                        charm=charmhub_client.charm_from_store(sample_independent_charm, "amd64"),
                    )
                }
            ),
            integrations=frozenset(),
            platform="kubernetes",
            arch="amd64",
        )
    )

    # THEN the config option exists in the file bundle
    assert all(
        (("config-option", "config-value"),) == application.config
        for application in minimal_bundle.applications
        if application.charm == sample_independent_charm
    )
