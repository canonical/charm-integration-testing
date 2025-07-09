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


import pytest
from pydantic import Field
from pydantic.dataclasses import dataclass

from bundle_builder.charmhub import CharmhubClient, CharmReleaseNotFoundException
from bundle_builder.charmhub_http import (
    CharmhubBase,
    FindResponse,
    RefreshAction,
    RefreshResponse,
)


@dataclass
class CharmhubHttpRefreshStub:
    refresh_charm: str
    refresh_base: CharmhubBase
    refresh_info: RefreshResponse

    def refresh(self, action: RefreshAction) -> RefreshResponse:
        assert action.charm_name == self.refresh_charm
        assert action.base == self.refresh_base
        return self.refresh_info


@dataclass
class CharmhubHttpFindStub:
    find_response: list[FindResponse]

    def find(self, provides: str | None = None, requires: str | None = None) -> list[FindResponse]:
        return self.find_response


@dataclass
class OverridesStub:
    charm_platform_overrides: dict[str, set[str]]

    def get_charm_platform_overrides(self, charm: str) -> set[str]:
        return self.charm_platform_overrides.get(charm, set())


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
        def test(self, params: Params):
            # GIVEN
            http_client = CharmhubHttpRefreshStub(
                refresh_charm=params.charm,
                refresh_base=CharmhubBase(name="NA", architecture=params.arch, channel="NA"),
                refresh_info=params.refresh_info,
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

    class TestSuitableCharmChannel:
        @dataclass
        class Params:
            label: str
            charm: str
            base: CharmhubBase
            refresh_info: RefreshResponse
            channel: str | None = None
            raise_exception: bool = False

        test_cases = [
            Params(
                label="successful_refresh",
                charm="my-charm",
                base=matching_base,
                refresh_info=RefreshResponse(
                    name="my-charm",
                    effective_channel="stable",
                    error=None,
                ),
                channel="stable",
            ),
            Params(
                label="channel_from_matching_release",
                charm="my-charm",
                base=matching_base,
                refresh_info=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="revision-not-found",
                        message="Missing revision",
                        extra=RefreshResponse.Error.Extra(
                            releases=[
                                RefreshResponse.Error.Extra.Release(channel="stable", base=matching_base),
                            ]
                        ),
                    ),
                ),
                channel="stable",
            ),
            Params(
                label="channel_from_matching_release_prefer_track",
                charm="my-charm",
                base=matching_base,
                refresh_info=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="revision-not-found",
                        message="Missing revision",
                        extra=RefreshResponse.Error.Extra(
                            releases=[
                                RefreshResponse.Error.Extra.Release(channel="edge", base=matching_base),
                                RefreshResponse.Error.Extra.Release(channel="latest/edge", base=matching_base),
                            ]
                        ),
                    ),
                ),
                channel="latest/edge",
            ),
            Params(
                label="no_matching_release_for_channel",
                charm="my-charm",
                base=matching_base,
                refresh_info=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="revision-not-found",
                        message="Missing revision",
                        extra=RefreshResponse.Error.Extra(
                            releases=[
                                RefreshResponse.Error.Extra.Release(channel="stable", base=other_base),
                            ]
                        ),
                    ),
                ),
                raise_exception=True,
            ),
            Params(
                label="refresh_info_error",
                charm="my-charm",
                base=matching_base,
                refresh_info=RefreshResponse(
                    name="my-charm",
                    error=RefreshResponse.Error(
                        code="unknown-error",
                        message="Missing revision",
                    ),
                ),
                raise_exception=True,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN
            http_client = CharmhubHttpRefreshStub(
                refresh_charm=params.charm,
                refresh_base=params.base,
                refresh_info=params.refresh_info,
            )

            # WHEN
            try:
                channel = CharmhubClient(http_client=http_client)._suitable_charm_channel(
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
                assert channel == params.channel

    class TestFindCharms:
        @dataclass
        class Params:
            label: str
            provides: str | None = None
            requires: str | None = None
            platform: str | None = None
            charms: list[FindResponse] = Field(default_factory=list)
            overrides: dict[str, set[str]] = Field(default_factory=dict)
            expected: set[str] = Field(default_factory=set)

        test_cases = [
            Params(
                label="no_filtering",
                provides=None,
                requires=None,
                platform=None,
                charms=[
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                    FindResponse(
                        "charm-b", result=FindResponse.Result(deployable_on=frozenset({"kubernetes", "machine"}))
                    ),
                ],
                overrides={},
                expected={"charm-a", "charm-b"},
            ),
            Params(
                label="platform_match_store_only",
                provides=None,
                requires=None,
                platform="machine",
                charms=[
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                    FindResponse(
                        "charm-b", result=FindResponse.Result(deployable_on=frozenset({"kubernetes", "machine"}))
                    ),
                ],
                overrides={},
                expected={"charm-b"},
            ),
            Params(
                label="platform_match_override_only",
                provides=None,
                requires=None,
                platform="machine",
                charms=[
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                    FindResponse("charm-b", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                ],
                overrides={
                    "charm-a": {"machine"},
                },
                expected={"charm-a"},
            ),
            Params(
                label="platform_match_store_or_override",
                platform="machine",
                charms=[
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"machine"}))),
                    FindResponse("charm-b", result=FindResponse.Result(deployable_on=frozenset({"kubernetes"}))),
                ],
                overrides={
                    "charm-b": {"machine"},
                },
                expected={"charm-a", "charm-b"},
            ),
            Params(
                label="no_matches_with_platform",
                platform="kubernetes",
                charms=[
                    FindResponse("charm-a", result=FindResponse.Result(deployable_on=frozenset({"machine"}))),
                ],
                overrides={},
                expected=set(),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN
            http_client = CharmhubHttpFindStub(find_response=params.charms)
            overrides_client = OverridesStub(charm_platform_overrides=params.overrides)

            # WHEN
            result = CharmhubClient(http_client=http_client, overrides_client=overrides_client).find_charms(
                provides=params.provides,
                requires=params.requires,
                platform=params.platform,
            )

            # THEN
            assert result == frozenset(params.expected)
