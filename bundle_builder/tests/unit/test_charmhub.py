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
from pydantic.dataclasses import dataclass

from bundle_builder.charmhub import CharmhubClient, CharmReleaseNotFoundException
from bundle_builder.charmhub_http import (
    CharmhubBase,
    RefreshAction,
    RefreshResponse,
)


@dataclass
class CharmhubHttpClientStub:
    refresh_charm: str
    refresh_base: CharmhubBase
    refresh_info: RefreshResponse

    def refresh(self, action: RefreshAction) -> RefreshResponse:
        assert action.charm_name == self.refresh_charm
        assert action.base == self.refresh_base
        return self.refresh_info


class TestCharmhubClient:
    class TestSuitableCharmChannel:
        @dataclass
        class Params:
            label: str
            charm: str
            base: CharmhubBase
            refresh_info: RefreshResponse
            channel: str | None = None
            raise_exception: bool = False

        matching_base = CharmhubBase(name="ubuntu", architecture="amd64", channel="20.04")
        other_base = CharmhubBase(name="ubuntu", architecture="amd64", channel="22.04")

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
                                RefreshResponse.Error.Extra.Release(channel="stable", base=matching_base),
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
            http_client = CharmhubHttpClientStub(
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
