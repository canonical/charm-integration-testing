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


from typing import Any

import pytest
import yaml
from pydantic import Field, TypeAdapter
from pydantic.dataclasses import dataclass

from bundle_builder.charm import CharmConfigCriteria, CharmTestConfig
from bundle_builder.charmhub import CharmhubClient
from bundle_builder.charmhub_http import (
    CharmhubBase,
    CharmhubHttpClient,
    CharmMetadata,
    CharmReleaseNotFoundException,
    FindResponse,
    InfoResponse,
    RefreshAction,
    RefreshResponse,
)
from bundle_builder.overrides import CharmTestConfigs, OverridesClient


@dataclass
class CharmhubHttpStub(CharmhubHttpClient):
    refresh_response: dict[RefreshAction, RefreshResponse] = Field(default_factory=dict)

    def refresh(self, action: RefreshAction) -> RefreshResponse:  # type: ignore[override]
        return self.refresh_response[action]

    find_response: list[FindResponse] = Field(default_factory=list)

    def find(self, provides: str | None = None, requires: str | None = None) -> list[FindResponse]:  # type: ignore[override]
        return self.find_response

    info_response: dict[str, InfoResponse] = Field(default_factory=dict)

    def info(self, charm: str) -> InfoResponse:  # type: ignore[override]
        return self.info_response[charm]


@dataclass
class OverridesStub(OverridesClient):
    charm_platform_overrides: dict[str, set[str] | None] = Field(default_factory=dict)  # type: ignore[assignment]

    def get_charm_platform_overrides(self, charm: str) -> set[str] | None:  # type: ignore[override]
        return self.charm_platform_overrides.get(charm, None)

    charm_listing_overrides: set[str] = Field(default_factory=set)  # type: ignore[assignment]

    def get_charm_listing_overrides(self) -> set[str]:  # type: ignore[override]
        return self.charm_listing_overrides

    charm_test_configs: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)  # type: ignore[assignment]

    def get_charm_test_configs(self, charm: str) -> list[CharmTestConfig]:  # type: ignore[override]
        configs = self.charm_test_configs.get(charm, [])
        # Use CharmTestConfigs wrapper to let Pydantic validate the list properly
        return CharmTestConfigs(configs=[CharmTestConfig(**c) for c in configs]).configs


matching_base = CharmhubBase(name="ubuntu", architecture="amd64", channel="20.04")
other_base = CharmhubBase(name="ubuntu", architecture="amd64", channel="22.04")


class TestCharmhubClient:
    class TestDefaultUbuntuVersion:
        @dataclass
        class Params:
            label: str
            charm: str
            arch: str
            channel: str | None
            refresh_info: RefreshResponse
            expected_version: str | None = None
            raise_exception: bool = False

        test_cases = [
            Params(
                label="default_bases_from_invalid_base_error",
                charm="my-charm",
                arch="amd64",
                channel="edge",
                refresh_info=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="invalid-charm-base",
                        message="Invalid base",
                        extra=RefreshResponse.Error.Extra(default_bases=[matching_base]),
                    ),
                ),
                expected_version=matching_base.channel,
            ),
            Params(
                label="default_bases_from_releases_in_revision_not_found",
                charm="my-charm",
                arch="amd64",
                channel=None,
                refresh_info=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="revision-not-found",
                        message="Missing revision",
                        extra=RefreshResponse.Error.Extra(
                            releases=[
                                RefreshResponse.Error.Extra.Release(channel="stable", base=matching_base),
                                RefreshResponse.Error.Extra.Release(channel="edge", base=other_base),
                            ]
                        ),
                    ),
                ),
                expected_version=matching_base.channel,
            ),
            Params(
                label="no_default_bases_found",
                charm="my-charm",
                arch="amd64",
                channel="beta",
                refresh_info=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="invalid-charm-base",
                        message="Invalid base",
                        extra=RefreshResponse.Error.Extra(default_bases=[]),
                    ),
                ),
                raise_exception=True,
            ),
            Params(
                label="unexpected_error_code_raises_exception",
                charm="my-charm",
                arch="amd64",
                channel="beta",
                refresh_info=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="some-other-error",
                        message="Something went wrong",
                    ),
                ),
                raise_exception=True,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN
            http_client = CharmhubHttpStub(
                refresh_response={
                    RefreshAction(
                        charm_name=params.charm,
                        charm_channel=params.channel,
                        base=CharmhubBase(name="NA", architecture=params.arch, channel="NA"),
                    ): params.refresh_info,
                },
            )

            # WHEN
            try:
                version = CharmhubClient(http_client=http_client)._default_ubuntu_version(
                    charm_name=params.charm,
                    ubuntu_arch=params.arch,
                    charm_channel=params.channel,
                )
            except CharmReleaseNotFoundException:
                raised = True
            else:
                raised = False

            # THEN
            if params.raise_exception:
                assert raised
            else:
                assert not raised
                assert version == params.expected_version

    class TestDefaultRefreshInfo:
        @dataclass
        class Params:
            label: str
            charm: str
            base: CharmhubBase
            refresh_info: dict[RefreshAction, RefreshResponse] = Field(default_factory=dict)
            expected_refresh_info: RefreshResponse | None = None
            raise_exception: bool = False

        test_cases = [
            Params(
                label="successful_refresh",
                charm="my-charm",
                base=matching_base,
                refresh_info={
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                    ): RefreshResponse(
                        name="my-charm",
                        effective_channel="stable",
                    ),
                },
                expected_refresh_info=RefreshResponse(
                    name="my-charm",
                    effective_channel="stable",
                ),
            ),
            Params(
                label="refresh_error",
                charm="my-charm",
                base=matching_base,
                refresh_info={
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                    ): RefreshResponse(
                        name="my-charm",
                        error=RefreshResponse.Error(message="Error Message", code="error-code"),
                    ),
                },
                raise_exception=True,
            ),
            Params(
                label="from_matching_release",
                charm="my-charm",
                base=matching_base,
                refresh_info={
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                    ): RefreshResponse(
                        name="my-charm",
                        error=RefreshResponse.Error(
                            code="revision-not-found",
                            message="Missing revision",
                            extra=RefreshResponse.Error.Extra(
                                releases=[
                                    RefreshResponse.Error.Extra.Release(channel="stable", base=matching_base),
                                    RefreshResponse.Error.Extra.Release(channel="edge", base=matching_base),
                                ]
                            ),
                        ),
                    ),
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                        charm_channel="stable",
                    ): RefreshResponse(
                        name="my-charm",
                        effective_channel="stable",
                    ),
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                        charm_channel="latest/stable",
                    ): RefreshResponse(
                        name="my-charm",
                        error=RefreshResponse.Error(message="Error Message", code="error-code"),
                    ),
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                        charm_channel="latest/edge",
                    ): RefreshResponse(
                        name="my-charm",
                        error=RefreshResponse.Error(message="Error Message", code="error-code"),
                    ),
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                        charm_channel="edge",
                    ): RefreshResponse(
                        name="my-charm",
                        error=RefreshResponse.Error(message="Error Message", code="error-code"),
                    ),
                },
                expected_refresh_info=RefreshResponse(
                    name="my-charm",
                    effective_channel="stable",
                ),
            ),
            Params(
                label="matching_release_error",
                charm="my-charm",
                base=matching_base,
                refresh_info={
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                    ): RefreshResponse(
                        name="my-charm",
                        error=RefreshResponse.Error(
                            code="revision-not-found",
                            message="Missing revision",
                            extra=RefreshResponse.Error.Extra(
                                releases=[
                                    RefreshResponse.Error.Extra.Release(channel="latest/edge", base=matching_base),
                                ]
                            ),
                        ),
                    ),
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                        charm_channel="latest/edge",
                    ): RefreshResponse(
                        name="my-charm",
                        error=RefreshResponse.Error(message="Error Message", code="error-code"),
                    ),
                },
                raise_exception=True,
            ),
            Params(
                label="try_adding_latest_track",
                charm="my-charm",
                base=matching_base,
                refresh_info={
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                    ): RefreshResponse(
                        name="my-charm",
                        error=RefreshResponse.Error(
                            code="revision-not-found",
                            message="Missing revision",
                            extra=RefreshResponse.Error.Extra(
                                releases=[
                                    RefreshResponse.Error.Extra.Release(channel="edge", base=matching_base),
                                ]
                            ),
                        ),
                    ),
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                        charm_channel="edge",
                    ): RefreshResponse(
                        name="my-charm",
                        error=RefreshResponse.Error(message="Error Message", code="error-code"),
                    ),
                    RefreshAction(
                        charm_name="my-charm",
                        base=matching_base,
                        charm_channel="latest/edge",
                    ): RefreshResponse(
                        name="my-charm",
                        effective_channel="latest/edge",
                    ),
                },
                expected_refresh_info=RefreshResponse(
                    name="my-charm",
                    effective_channel="latest/edge",
                ),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN
            http_client = CharmhubHttpStub(refresh_response=params.refresh_info)

            # WHEN
            try:
                actual_refresh_info = CharmhubClient(http_client=http_client)._default_refresh_info(
                    params.charm, base=params.base
                )
            except CharmReleaseNotFoundException:
                raised_charm_release_not_found = True
            else:
                raised_charm_release_not_found = False

            # THEN
            if params.raise_exception:
                assert raised_charm_release_not_found
            else:
                assert not raised_charm_release_not_found
                assert actual_refresh_info == params.expected_refresh_info

    class TestFindCharms:
        @dataclass
        class Params:
            label: str
            provides: str | None = None
            requires: str | None = None
            platform: str | None = None
            find_response: list[FindResponse] = Field(default_factory=list)
            info_response: dict[str, InfoResponse] = Field(default_factory=dict)
            platform_overrides: dict[str, set[str] | None] = Field(default_factory=dict)
            listing_overrides: set[str] = Field(default_factory=set)
            expected: set[str] = Field(default_factory=set)

        test_cases = [
            Params(
                label="success",
                find_response=[
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                    FindResponse(
                        "charm-b", result=FindResponse.Result(deployable_on=frozenset({"kubernetes", "machine"}))
                    ),
                ],
                expected={"charm-a", "charm-b"},
            ),
            Params(
                label="listing_override",
                find_response=[],
                listing_overrides={"charm-a"},
                info_response={
                    "charm-a": InfoResponse(result=InfoResponse.Result(deployable_on=frozenset({"kubernetes"})))
                },
                expected={"charm-a"},
            ),
            Params(
                label="filter_platform",
                platform="machine",
                find_response=[
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                    FindResponse(
                        "charm-b", result=FindResponse.Result(deployable_on=frozenset({"kubernetes", "machine"}))
                    ),
                ],
                expected={"charm-b"},
            ),
            Params(
                label="platform_override",
                platform="machine",
                find_response=[
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                ],
                platform_overrides={"charm-a": {"machine"}},
                expected={"charm-a"},
            ),
            Params(
                label="empty_deployable_on_defaults_to_machine",
                find_response=[
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset())),
                    FindResponse("charm-b", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                ],
                expected={"charm-a", "charm-b"},
            ),
            Params(
                label="empty_deployable_on_filtered_by_platform",
                platform="machine",
                find_response=[
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset())),
                    FindResponse("charm-b", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                ],
                expected={"charm-a"},
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN
            http_client = CharmhubHttpStub(find_response=params.find_response, info_response=params.info_response)
            overrides_client = OverridesStub(
                charm_platform_overrides=params.platform_overrides, charm_listing_overrides=params.listing_overrides
            )

            # WHEN
            actual = CharmhubClient(
                http_client=http_client,
                overrides_client=overrides_client,
            ).find_charms(
                provides=params.provides,
                requires=params.requires,
                platform=params.platform,
            )

            # THEN
            assert actual == params.expected

    class TestFindCharmsGetListingOverrides:
        @dataclass
        class Params:
            label: str
            provides: str | None = None
            requires: str | None = None
            listing_overrides: set[str] = Field(default_factory=set)
            info_response: dict[str, InfoResponse] = Field(default_factory=dict)
            expected: set[FindResponse] = Field(default_factory=set)

        test_cases = [
            Params(
                label="no_overrides",
                expected=set(),
            ),
            Params(
                label="with_overrides",
                listing_overrides={"charm-a"},
                info_response={
                    "charm-a": InfoResponse(result=InfoResponse.Result(deployable_on=frozenset({"kubernetes"})))
                },
                expected={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                },
            ),
            Params(
                label="filter_provides",
                provides="interface-a",
                listing_overrides={"charm-a", "charm-b"},
                info_response={
                    "charm-a": InfoResponse(
                        default_release=InfoResponse.DefaultRelease(
                            revision=TypeAdapter(InfoResponse.DefaultRelease.Revision).validate_python({
                                "metadata-yaml": yaml.dump(
                                    {
                                        "provides": {
                                            "endpoint-a": {
                                                "interface": "interface-a",
                                            }
                                        }
                                    }
                                ),
                            })
                        ),
                        result=InfoResponse.Result(deployable_on=frozenset({"kubernetes"})),
                    ),
                    "charm-b": InfoResponse(
                        result=InfoResponse.Result(deployable_on=frozenset({"kubernetes"})),
                    ),
                },
                expected={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                },
            ),
            Params(
                label="filter_requires",
                requires="interface-a",
                listing_overrides={"charm-a", "charm-b"},
                info_response={
                    "charm-a": InfoResponse(
                        default_release=InfoResponse.DefaultRelease(
                            revision=TypeAdapter(InfoResponse.DefaultRelease.Revision).validate_python({
                                "metadata-yaml": yaml.dump({
                                        "requires": {
                                            "endpoint-a": {
                                                "interface": "interface-a",
                                            }
                                        }
                                }),
                            })
                        ),
                        result=InfoResponse.Result(deployable_on=frozenset({"kubernetes"})),
                    ),
                    "charm-b": InfoResponse(
                        result=InfoResponse.Result(deployable_on=frozenset({"kubernetes"})),
                    ),
                },
                expected={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                },
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN
            http_client = CharmhubHttpStub(info_response=params.info_response)
            overrides_client = OverridesStub(charm_listing_overrides=params.listing_overrides)

            # WHEN
            actual = CharmhubClient(
                http_client=http_client,
                overrides_client=overrides_client,
            )._find_charms_get_listing_overrides(
                provides=params.provides,
                requires=params.requires,
            )

            # THEN
            assert actual == params.expected

    class TestFindCharmsAddPlatformOverrides:
        @dataclass
        class Params:
            label: str
            given: set[FindResponse] = Field(default_factory=set)
            platform_overrides: dict[str, set[str] | None] = Field(default_factory=dict)
            expected: set[FindResponse] = Field(default_factory=set)

        test_cases = [
            Params(
                label="no_overrides",
                given={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                },
                expected={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                },
            ),
            Params(
                label="apply_overrides",
                given={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                },
                platform_overrides={"charm-a": {"machine"}},
                expected={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"machine"}))),
                },
            ),
            Params(
                label="override_replaces_not_extends",
                given={
                    FindResponse(
                        "charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes", "machine"}))
                    ),
                },
                platform_overrides={"charm-a": {"machine"}},
                expected={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"machine"}))),
                },
            ),
            Params(
                label="none_override_skips_charm",
                given={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                },
                platform_overrides={"charm-a": None},
                expected={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                },
            ),
            Params(
                label="multiple_charms_mixed_overrides",
                given={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                    FindResponse("charm-b", result=FindResponse.Result(deployable_on=frozenset({"machine"}))),
                    FindResponse(
                        "charm-c", result=FindResponse.Result(deployable_on=frozenset({"kubernetes", "machine"}))
                    ),
                },
                platform_overrides={"charm-a": {"machine"}, "charm-b": None},
                expected={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"machine"}))),
                    FindResponse("charm-b", result=FindResponse.Result(deployable_on=frozenset({"machine"}))),
                    FindResponse(
                        "charm-c", result=FindResponse.Result(deployable_on=frozenset({"kubernetes", "machine"}))
                    ),
                },
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN
            overrides_client = OverridesStub(charm_platform_overrides=params.platform_overrides)

            # WHEN
            actual = CharmhubClient(overrides_client=overrides_client)._find_charms_add_platform_overrides(params.given)

            # THEN
            assert actual == params.expected

    class TestFindCharmsAddDeployableOnOverrides:
        @dataclass
        class Params:
            label: str
            given: set[FindResponse] = Field(default_factory=set)
            expected: set[FindResponse] = Field(default_factory=set)

        test_cases = [
            Params(
                label="empty_deployable_on_defaults_to_machine",
                given={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset())),
                },
                expected={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"machine"}))),
                },
            ),
            Params(
                label="existing_deployable_on_preserved",
                given={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                },
                expected={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                },
            ),
            Params(
                label="mixed_empty_and_existing_deployable_on",
                given={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset())),
                    FindResponse(
                        "charm-b", result=FindResponse.Result(deployable_on=frozenset({"kubernetes", "machine"}))
                    ),
                },
                expected={
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"machine"}))),
                    FindResponse(
                        "charm-b", result=FindResponse.Result(deployable_on=frozenset({"kubernetes", "machine"}))
                    ),
                },
            ),
            Params(
                label="empty_response_set",
                given=set(),
                expected=set(),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN
            client = CharmhubClient()

            # WHEN
            actual = client._find_charms_add_deployable_on_overrides(params.given)

            # THEN
            assert actual == params.expected

    class TestCharmTestConfigs:
        @dataclass
        class Params:
            label: str
            charm: str
            charm_test_configs: dict[str, list[dict[str, Any]]]
            expected: tuple[CharmTestConfig, ...]

        test_cases = [
            Params(
                label="no_configs",
                charm="charm-a",
                charm_test_configs={"charm-a": []},
                expected=(),
            ),
            Params(
                label="single_config",
                charm="charm-a",
                charm_test_configs={"charm-a": [{"config": {"key1": "val1", "key2": 2}}]},
                expected=(
                    CharmTestConfig(
                        criteria=CharmConfigCriteria.from_bool(True),
                        config=(("key1", "val1"), ("key2", 2)),
                    ),
                ),
            ),
            Params(
                label="multiple_configs",
                charm="charm-b",
                charm_test_configs={"charm-b": [{"config": {"a": "x"}}, {"config": {"b": "y"}}]},
                expected=(
                    CharmTestConfig(
                        criteria=CharmConfigCriteria.from_bool(True),
                        config=(("a", "x"),),
                    ),
                    CharmTestConfig(
                        criteria=CharmConfigCriteria.from_bool(True),
                        config=(("b", "y"),),
                    ),
                ),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN
            overrides_client = OverridesStub(charm_test_configs=params.charm_test_configs)
            client = CharmhubClient(overrides_client=overrides_client)

            # WHEN
            actual = client._charm_test_configs(params.charm)

            # THEN
            assert actual == params.expected

    class TestGetUbuntuVersionFromBases:
        @dataclass
        class Params:
            label: str
            bases: list[CharmhubBase]
            ubuntu_arch: str
            charm_name: str
            charm_revision: int
            ubuntu_version: str | None
            expected_version: str | None = None
            raise_exception: bool = False

        test_cases = [
            Params(
                label="returns_first_matching_base",
                bases=[matching_base, other_base],
                ubuntu_arch="amd64",
                charm_name="test-charm",
                charm_revision=42,
                ubuntu_version=None,
                expected_version="20.04",
            ),
            Params(
                label="validates_provided_version_exists",
                bases=[matching_base, other_base],
                ubuntu_arch="amd64",
                charm_name="test-charm",
                charm_revision=42,
                ubuntu_version="22.04",
                expected_version="22.04",
            ),
            Params(
                label="raises_when_provided_version_not_in_bases",
                bases=[matching_base],
                ubuntu_arch="amd64",
                charm_name="test-charm",
                charm_revision=42,
                ubuntu_version="24.04",
                raise_exception=True,
            ),
            Params(
                label="raises_when_no_matching_arch",
                bases=[CharmhubBase(name="ubuntu", architecture="arm64", channel="20.04")],
                ubuntu_arch="amd64",
                charm_name="test-charm",
                charm_revision=42,
                ubuntu_version=None,
                raise_exception=True,
            ),
            Params(
                label="raises_when_empty_bases",
                bases=[],
                ubuntu_arch="amd64",
                charm_name="test-charm",
                charm_revision=42,
                ubuntu_version=None,
                raise_exception=True,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN
            client = CharmhubClient()

            # WHEN
            try:
                version = client._get_ubuntu_version_from_bases(
                    params.bases,
                    params.ubuntu_arch,
                    params.charm_name,
                    params.charm_revision,
                    params.ubuntu_version,
                )
            except CharmReleaseNotFoundException:
                raised = True
            else:
                raised = False

            # THEN
            if params.raise_exception:
                assert raised
            else:
                assert not raised
                assert version == params.expected_version

    class TestGetRevisionRefreshInfo:
        @dataclass
        class Params:
            label: str
            charm_name: str
            charm_revision: int
            refresh_response: RefreshResponse
            raise_exception: bool = False

        test_cases = [
            Params(
                label="successful_refresh",
                charm_name="my-charm",
                charm_revision=123,
                refresh_response=RefreshResponse(
                    name="my-charm",
                    effective_channel="stable",
                    charm=RefreshResponse.Charm(
                        revision=123,
                        bases=[matching_base],
                    ),
                ),
            ),
            Params(
                label="raises_on_error",
                charm_name="my-charm",
                charm_revision=999,
                refresh_response=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="revision-not-found",
                        message="Revision not found",
                    ),
                ),
                raise_exception=True,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN
            http_client = CharmhubHttpStub(
                refresh_response={
                    RefreshAction(
                        charm_name=params.charm_name,
                        charm_revision=params.charm_revision,
                        always_include_base=True,
                    ): params.refresh_response,
                }
            )
            client = CharmhubClient(http_client=http_client)

            # WHEN
            try:
                result = client._get_revision_refresh_info(params.charm_name, params.charm_revision)
            except CharmReleaseNotFoundException:
                raised = True
            else:
                raised = False

            # THEN
            if params.raise_exception:
                assert raised
            else:
                assert not raised
                assert result == params.refresh_response

    class TestSupportedUbuntuVersions:
        @dataclass
        class Params:
            label: str
            charm_name: str
            ubuntu_arch: str
            charm_channel: str | None
            refresh_response: RefreshResponse
            expected_versions: list[str]
            raise_exception: bool = False

        test_cases = [
            Params(
                label="returns_default_bases_from_error",
                charm_name="my-charm",
                ubuntu_arch="amd64",
                charm_channel="stable",
                refresh_response=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="invalid-charm-base",
                        message="Invalid base",
                        extra=RefreshResponse.Error.Extra(default_bases=[matching_base, other_base]),
                    ),
                ),
                expected_versions=["20.04", "22.04"],
            ),
            Params(
                label="filters_non_ubuntu_bases",
                charm_name="my-charm",
                ubuntu_arch="amd64",
                charm_channel=None,
                refresh_response=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="invalid-charm-base",
                        message="Invalid base",
                        extra=RefreshResponse.Error.Extra(
                            default_bases=[
                                matching_base,
                                CharmhubBase(name="centos", architecture="amd64", channel="7"),
                            ]
                        ),
                    ),
                ),
                expected_versions=["20.04"],
            ),
            Params(
                label="raises_on_unexpected_error",
                charm_name="my-charm",
                ubuntu_arch="amd64",
                charm_channel="edge",
                refresh_response=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="some-other-error",
                        message="Unexpected error",
                    ),
                ),
                expected_versions=[],
                raise_exception=True,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN
            http_client = CharmhubHttpStub(
                refresh_response={
                    RefreshAction(
                        charm_name=params.charm_name,
                        charm_channel=params.charm_channel,
                        base=CharmhubBase(name="NA", architecture=params.ubuntu_arch, channel="NA"),
                    ): params.refresh_response,
                }
            )
            client = CharmhubClient(http_client=http_client)

            # WHEN
            try:
                versions = client._supported_ubuntu_versions(
                    params.charm_name, params.ubuntu_arch, charm_channel=params.charm_channel
                )
            except CharmReleaseNotFoundException:
                raised = True
            else:
                raised = False

            # THEN
            if params.raise_exception:
                assert raised
            else:
                assert not raised
                assert versions == params.expected_versions

    class TestCharmFromStoreByChannelAndRevision:
        @dataclass
        class Params:
            label: str
            charm_name: str
            ubuntu_arch: str
            charm_channel: str
            charm_revision: int
            ubuntu_version: str | None
            revision_refresh_response: RefreshResponse
            supported_versions_refresh_response: RefreshResponse
            raise_exception: bool = False

        test_cases = [
            Params(
                label="successful_with_matching_base",
                charm_name="test-charm",
                ubuntu_arch="amd64",
                charm_channel="stable",
                charm_revision=42,
                ubuntu_version=None,
                revision_refresh_response=RefreshResponse(
                    name="test-charm",
                    effective_channel="stable",
                    charm=RefreshResponse.Charm(
                        revision=42,
                        bases=[matching_base],
                        config=yaml.dump({"provides": {"endpoint-a": {"interface": "interface-a"}}}),
                        metadata=CharmMetadata({"provides": {"endpoint-a": {"interface": "interface-a"}}}),
                    ),
                ),
                supported_versions_refresh_response=RefreshResponse(
                    name="test-charm",
                    error=RefreshResponse.Error(
                        code="invalid-charm-base",
                        message="Invalid base",
                        extra=RefreshResponse.Error.Extra(default_bases=[matching_base]),
                    ),
                ),
            ),
            Params(
                label="raises_when_channel_does_not_support_base",
                charm_name="test-charm",
                ubuntu_arch="amd64",
                charm_channel="edge",
                charm_revision=99,
                ubuntu_version=None,
                revision_refresh_response=RefreshResponse(
                    name="test-charm",
                    effective_channel="stable",
                    charm=RefreshResponse.Charm(
                        revision=99,
                        bases=[matching_base],
                        config=yaml.dump({}),
                        metadata=CharmMetadata({}),
                    ),
                ),
                supported_versions_refresh_response=RefreshResponse(
                    name="test-charm",
                    error=RefreshResponse.Error(
                        code="invalid-charm-base",
                        message="Invalid base",
                        extra=RefreshResponse.Error.Extra(default_bases=[other_base]),
                    ),
                ),
                raise_exception=True,
            ),
            Params(
                label="validates_provided_ubuntu_version",
                charm_name="test-charm",
                ubuntu_arch="amd64",
                charm_channel="stable",
                charm_revision=50,
                ubuntu_version="22.04",
                revision_refresh_response=RefreshResponse(
                    name="test-charm",
                    effective_channel="stable",
                    charm=RefreshResponse.Charm(
                        revision=50,
                        bases=[matching_base, other_base],
                        config=yaml.dump({}),
                        metadata=CharmMetadata({}),
                    ),
                ),
                supported_versions_refresh_response=RefreshResponse(
                    name="test-charm",
                    error=RefreshResponse.Error(
                        code="invalid-charm-base",
                        message="Invalid base",
                        extra=RefreshResponse.Error.Extra(default_bases=[matching_base, other_base]),
                    ),
                ),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN
            http_client = CharmhubHttpStub(
                refresh_response={
                    RefreshAction(
                        charm_name=params.charm_name,
                        charm_revision=params.charm_revision,
                        always_include_base=True,
                    ): params.revision_refresh_response,
                    RefreshAction(
                        charm_name=params.charm_name,
                        charm_channel=params.charm_channel,
                        base=CharmhubBase(name="NA", architecture=params.ubuntu_arch, channel="NA"),
                    ): params.supported_versions_refresh_response,
                }
            )
            client = CharmhubClient(http_client=http_client)

            # WHEN
            try:
                charm = client._charm_from_store_by_channel_and_revision(
                    charm_name=params.charm_name,
                    ubuntu_arch=params.ubuntu_arch,
                    charm_channel=params.charm_channel,
                    charm_revision=params.charm_revision,
                    ubuntu_version=params.ubuntu_version,
                )
            except CharmReleaseNotFoundException:
                raised = True
            else:
                raised = False

            # THEN
            if params.raise_exception:
                assert raised
            else:
                assert not raised
                assert charm.name == params.charm_name
                assert str(charm.channel) == params.charm_channel
                assert charm.revision == params.charm_revision
                assert charm.ubuntu_arch == params.ubuntu_arch
