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
from typing import Any

import pytest
import yaml
from pydantic import Field
from pydantic.dataclasses import dataclass

from bundle_builder.charm import (
    CharmConfigCriteria,
    CharmEndpointOptionality,
    CharmLimit,
    CharmLimitCriteria,
    CharmTestConfig,
)
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
                    all_of=frozenset(
                        [
                            CharmEndpointOptionality.from_bool(True),
                            CharmEndpointOptionality.from_bool(False),
                        ]
                    )
                ),
            ),
            Params(
                label="not_set",
                override=CharmEndpointOverride(),
                optionality=None,
            ),
        ]

    class TestLimit:
        @dataclass
        class Params:
            label: str
            override: CharmEndpointOverride
            expected_limits: tuple[CharmLimit, ...] | None

        test_cases = [
            Params(
                label="limit_set",
                override=CharmEndpointOverride(limit=5),
                expected_limits=(CharmLimit(limit=5),),
            ),
            Params(
                label="limit_zero",
                override=CharmEndpointOverride(limit=0),
                expected_limits=(CharmLimit(limit=0),),
            ),
            Params(
                label="limit_not_set",
                override=CharmEndpointOverride(),
                expected_limits=None,
            ),
            Params(
                label="limit_if_only",
                override=CharmEndpointOverride(
                    limit_if=[
                        CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"), limit=10),
                        CharmLimit(limit=1),
                    ]
                ),
                expected_limits=(
                    CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"), limit=10),
                    CharmLimit(limit=1),
                ),
            ),
            Params(
                label="limit_and_limit_if_combined",
                override=CharmEndpointOverride(
                    limit=5,
                    limit_if=[
                        CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"), limit=10),
                    ],
                ),
                expected_limits=(
                    CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"), limit=10),
                    CharmLimit(limit=5),
                ),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN the override
            override = params.override

            # WHEN limits property is fetched
            limits = override.limits

            # THEN matches expected
            assert limits == params.expected_limits

    class TestLimitsProperty:
        """Test the limits property logic that combines limit and limit_if."""

        def test_limit_if_comes_before_limit(self) -> None:
            # GIVEN an override with both limit and limit_if
            override = CharmEndpointOverride(
                limit=1,
                limit_if=[
                    CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"), limit=10),
                ],
            )

            # WHEN getting limits
            limits = override.limits

            # THEN limit_if entries come first, followed by limit
            assert limits is not None
            assert len(limits) == 2
            assert limits[0].limit == 10  # From limit_if
            assert limits[1].limit == 1  # From limit


class TestOverridesClient:
    class TestGetCharmMetadataOverrides:
        @dataclass
        class Params:
            label: str
            charm: str = "postgresql-k8s"
            overrides: dict[str, Any] = Field(default_factory=dict)
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
            Params(
                label="override_with_limit",
                charm="limited-charm",
                overrides={
                    "limited-charm": {
                        "provides": {"database": {"limit": 3}},
                        "requires": {"certificates": {"limit": 1, "optional": True}},
                    },
                },
                expected_override=CharmMetadataOverride(
                    provides={"database": CharmEndpointOverride(limit=3)},
                    requires={"certificates": CharmEndpointOverride(limit=1, optional=True)},
                ),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params, tmp_path: Path) -> None:
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
            overrides: dict[str, Any] = Field(default_factory=dict)
            expected_override: set[str] | None = Field(default_factory=set)
            overrides_directory: bool = True

        test_cases = [
            Params(label="overrides_directory_not_given", overrides_directory=False, expected_override=None),
            Params(label="override_for_charm_not_found", overrides={}, expected_override=None),
            Params(
                label="override_is_provided",
                overrides={
                    "postgresql-k8s": {"platforms": ["kubernetes", "machine"]},
                },
                expected_override={"kubernetes", "machine"},
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params, tmp_path: Path) -> None:
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
            overrides: dict[str, Any] = Field(default_factory=dict)
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
        def test(self, params: Params, tmp_path: Path) -> None:
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
            overrides: dict[str, Any] = Field(default_factory=dict)
            expected: list[CharmTestConfig] = Field(default_factory=list)
            overrides_directory: bool = True

        test_cases = [
            Params(label="overrides_directory_not_given", overrides_directory=False, expected=[]),
            Params(label="override_for_charm_not_found", overrides={}, expected=[]),
            Params(
                label="override_is_provided",
                overrides={
                    "charm-a": {
                        "configs": [
                            {"config": {"a": 1, "b": "x"}},
                            {"config": {"c": 2}},
                        ]
                    }
                },
                expected=[
                    CharmTestConfig(
                        criteria=CharmConfigCriteria.from_bool(True),
                        config=(("a", 1), ("b", "x")),
                    ),
                    CharmTestConfig(
                        criteria=CharmConfigCriteria.from_bool(True),
                        config=(("c", 2),),
                    ),
                ],
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params, tmp_path: Path) -> None:
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

    class TestGetCharmPrioritiesMapping:
        @dataclass
        class Params:
            label: str
            overrides: dict[str, Any] = Field(default_factory=dict)
            expected_priorities: dict[str, float] = Field(default_factory=dict)
            supply_file: bool = True

        test_cases = [
            Params(label="overrides_file_not_given", supply_file=False, expected_priorities={}),
            Params(label="overrides_are_empty", overrides={"priorities": {}}, expected_priorities={}),
            Params(
                label="overrides_are_provided",
                overrides={"priorities": {"charm-a": 1.0, "charm-b": 0.5, "charm-c": 2.0}},
                expected_priorities={"charm-a": 1.0, "charm-b": 0.5, "charm-c": 2.0},
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params, tmp_path: Path) -> None:
            # GIVEN a yaml file
            override_file = tmp_path / "priorities.yaml"
            # AND its content according to params.overrides
            if params.supply_file:
                with override_file.open("w") as file:
                    yaml.dump(params.overrides, file)
            # AND an OverridesClient constructed from it
            overrides_client = OverridesClient(charm_priorities_config=override_file if params.supply_file else None)

            # WHEN the charm priorities mapping is retrieved
            actual = overrides_client.get_charm_priorities_mapping()

            # THEN the resulting priorities are as defined in the file
            assert actual == params.expected_priorities
