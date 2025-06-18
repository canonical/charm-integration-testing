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


from dataclasses import field
from pathlib import Path

import pytest
import yaml
from pydantic.dataclasses import dataclass

from bundle_builder.charm import CharmEndpointOptionality
from bundle_builder.overrides import CharmEndpointOverride, CharmMetadataOverride, OverridesClient


class TestCharmEndpointOverride:
    class TestOptionality:
        @dataclass
        class Params:
            label: str
            override: CharmEndpointOverride
            optionality: CharmEndpointOptionality | None

        test_cases = [
            Params(
                label="all_of",
                override=CharmEndpointOverride(
                    optional_if=[
                        CharmEndpointOptionality.from_bool(True),
                        CharmEndpointOptionality.from_bool(False),
                    ]
                ),
                optionality=CharmEndpointOptionality(
                    all_of=[
                        CharmEndpointOptionality.from_bool(True),
                        CharmEndpointOptionality.from_bool(False),
                    ]
                ),
            ),
            Params(
                label="not_set",
                override=CharmEndpointOverride(optional_if=None),
                optionality=None,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN the override
            override = params.override

            # WHEN optionality property is fetched
            optionality = override.optionality

            # THEN matches expected
            assert optionality == params.optionality


class TestOverridesClient:
    class TestGetCharmOverrides:
        @dataclass
        class Params:
            label: str
            charm: str = "postgresql-k8s"
            overrides: dict = field(default_factory=dict)
            expected_override: CharmMetadataOverride = field(default_factory=CharmMetadataOverride)
            overrides_directory: bool = True

        test_cases = [
            Params(
                label="overrides_directory_not_given",
                overrides_directory=False,
            ),
            Params(label="override_for_charm_not_found", charm="postgresql-k8s", overrides={}),
            Params(
                label="override_is_provided",
                charm="postgresql-k8s",
                overrides={
                    "postgresql-k8s": {
                        "requires": {
                            "certificates": {
                                "optional_if": [
                                    {"endpoint_integrated": "database"},
                                ]
                            }
                        },
                    },
                },
                expected_override=CharmMetadataOverride(
                    requires={
                        "certificates": CharmEndpointOverride(
                            optional_if=[CharmEndpointOptionality(endpoint_integrated="database")],
                        )
                    },
                ),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params, tmp_path: Path):
            # GIVEN the override client
            overrides_client = OverridesClient(
                charm_metadata_overrides=tmp_path if params.overrides_directory else None
            )
            # AND the provided overrides are written to files
            for charm, override in params.overrides.items():
                with (tmp_path / f"{charm}.yaml").open("w") as f:
                    yaml.dump(override, f)

            # WHEN the override for the charm is fetched
            override = overrides_client.get_charm_overrides(params.charm)

            # THEN matches expected
            assert override == params.expected_override
