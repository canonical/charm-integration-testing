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

import pytest
import yaml
from pydantic import Field
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
                label="optional_set",
                override=CharmEndpointOverride(optional=True),
                optionality=CharmEndpointOptionality.from_bool(True),
            ),
            Params(
                label="optional_if_set",
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
                override=CharmEndpointOverride(),
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
    class TestGetCharmMetadataOverrides:
        @dataclass
        class Params:
            label: str
            charm: str = "postgresql-k8s"
            overrides: dict = Field(default_factory=dict)
            expected_override: CharmMetadataOverride = Field(default_factory=CharmMetadataOverride)
            overrides_directory: bool = True

        test_cases = [
            Params(label="overrides_directory_not_given", overrides_directory=False),
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
            # GIVEN
            if params.overrides_directory:
                for charm, override in params.overrides.items():
                    with (tmp_path / f"{charm}.yaml").open("w") as file:
                        yaml.dump(override, file)
            # AND
            overrides_client = OverridesClient(
                charm_metadata_overrides=tmp_path if params.overrides_directory else None
            )

            # WHEN
            override = overrides_client.get_charm_metadata_overrides(params.charm)

            # THEN
            assert override == params.expected_override

    class TestGetCharmPlatformOverrides:
        @dataclass
        class Params:
            label: str
            charm: str = "postgresql-k8s"
            overrides: dict = Field(default_factory=dict)
            expected_override: set[str] = Field(default_factory=set)
            overrides_directory: bool = True

        test_cases = [
            Params(label="overrides_directory_not_given", overrides_directory=False),
            Params(label="override_for_charm_not_found", overrides={}),
            Params(
                label="override_is_provided",
                overrides={
                    "postgresql-k8s": {"platforms": ["kubernetes", "machine"]},
                },
                expected_override={"kubernetes", "machine"},
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params, tmp_path: Path):
            # GIVEN
            if params.overrides_directory:
                for charm, override in params.overrides.items():
                    with (tmp_path / f"{charm}.yaml").open("w") as file:
                        yaml.dump(override, file)
            # AND
            overrides_client = OverridesClient(
                charm_platform_overrides=tmp_path if params.overrides_directory else None
            )

            # WHEN
            actual = overrides_client.get_charm_platform_overrides(params.charm)

            # THEN
            assert actual == params.expected_override

    class TestGetCharmListingOverrides:
        @dataclass
        class Params:
            label: str
            overrides: dict = Field(default_factory=dict)
            expected_overrides: set[str] = Field(default_factory=set)
            supply_file: bool = True

        test_cases = [
            Params(label="overrides_file_not_given", supply_file=False),
            Params(label="overrides_are_empty", overrides={"unlisted_charms": []}, expected_overrides=set()),
            Params(
                label="overrides_are_provided",
                overrides={"unlisted_charms": ["charm-a", "charm-b"]},
                expected_overrides={"charm-a", "charm-b"},
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params, tmp_path: Path):
            # GIVEN
            override_file = tmp_path / "listing.yaml"
            # AND
            if params.supply_file:
                with override_file.open("w") as file:
                    yaml.dump(params.overrides, file)
            # AND
            overrides_client = OverridesClient(charm_listing_overrides=override_file if params.supply_file else None)

            # WHEN
            actual = overrides_client.get_charm_listing_overrides()

            # THEN
            assert actual == params.expected_overrides

    class TestGetCharmTestConfigs:
        @dataclass
        class Params:
            label: str
            charm: str = "charm-a"
            overrides: dict = Field(default_factory=dict)
            expected: list[dict] = Field(default_factory=list)
            overrides_directory: bool = True

        test_cases = [
            Params(label="overrides_directory_not_given", overrides_directory=False, expected=[]),
            Params(label="override_for_charm_not_found", overrides={}, expected=[]),
            Params(
                label="override_is_provided",
                overrides={
                    "charm-a": {
                        "configs": [
                            {"a": 1, "b": "x"},
                            {"c": 2},
                        ]
                    }
                },
                expected=[
                    {"a": 1, "b": "x"},
                    {"c": 2},
                ],
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params, tmp_path: Path):
            # GIVEN
            if params.overrides_directory:
                for charm, override in params.overrides.items():
                    with (tmp_path / f"{charm}.yaml").open("w") as file:
                        yaml.dump(override, file)
            # AND
            overrides_client = OverridesClient(charm_test_configs=tmp_path if params.overrides_directory else None)

            # WHEN
            actual = overrides_client.get_charm_test_configs(params.charm)

            # THEN
            assert actual == params.expected


class TestCharmEndpointOverrideValidation:
    def test_valid_limit_override(self):
        # GIVEN valid limit values
        override1 = CharmEndpointOverride(limit=1)
        override2 = CharmEndpointOverride(limit=0)
        override3 = CharmEndpointOverride(limit=None)

        # THEN no exceptions are raised
        assert override1.limit == 1
        assert override2.limit == 0
        assert override3.limit is None

    def test_invalid_limit_override(self):
        # WHEN creating override with negative limit
        # THEN ValueError is raised
        with pytest.raises(ValueError, match="Endpoint limit must be non-negative"):
            CharmEndpointOverride(limit=-1)

    def test_valid_inject_charm_override(self):
        # GIVEN valid inject_charm values for realistic postgresql-k8s -> pgbouncer-k8s scenario
        override1 = CharmEndpointOverride(inject_charm="pgbouncer-k8s")  # pgbouncer as connection pooler
        override2 = CharmEndpointOverride(inject_charm=None)
        override3 = CharmEndpointOverride(limit=1, inject_charm="pgbouncer-k8s")  # postgresql with limit=1 injects pgbouncer

        # THEN no exceptions are raised
        assert override1.inject_charm == "pgbouncer-k8s"
        assert override2.inject_charm is None
        assert override3.inject_charm == "pgbouncer-k8s"

    def test_invalid_inject_charm_override(self):
        # WHEN creating override with empty inject_charm
        # THEN ValueError is raised
        with pytest.raises(ValueError, match="inject_charm cannot be empty"):
            CharmEndpointOverride(inject_charm="")
        
        with pytest.raises(ValueError, match="inject_charm cannot be empty"):
            CharmEndpointOverride(inject_charm="   ")

    def test_inject_charm_with_limit_from_charmhub(self):
        # GIVEN inject_charm without explicit limit (should use CharmHub limit for postgresql-k8s)
        override = CharmEndpointOverride(inject_charm="pgbouncer-k8s")

        # THEN no exception is raised (limit will come from CharmHub)
        assert override.inject_charm == "pgbouncer-k8s"
        assert override.limit is None
