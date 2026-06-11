# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass

import pytest
from juju import CharmChannel, JujuIntegrationApplication


class TestCharmChannel:
    class TestParse:
        @dataclass
        class Params:
            label: str
            input: str | dict[str, str]
            expected_track: str
            expected_risk: str
            expected_branch: str
            should_raise: bool = False

        test_cases = [
            Params(
                label="risk_only",
                input="stable",
                expected_track="",
                expected_risk="stable",
                expected_branch="",
            ),
            Params(
                label="track_and_risk",
                input="1.0/stable",
                expected_track="1.0",
                expected_risk="stable",
                expected_branch="",
            ),
            Params(
                label="track_risk_branch",
                input="1.0/stable/fix-123",
                expected_track="1.0",
                expected_risk="stable",
                expected_branch="fix-123",
            ),
            Params(
                label="from_dict",
                input={"track": "2.0", "risk": "edge", "branch": ""},
                expected_track="2.0",
                expected_risk="edge",
                expected_branch="",
            ),
            Params(
                label="too_many_parts",
                input="a/b/c/d",
                expected_track="",
                expected_risk="",
                expected_branch="",
                should_raise=True,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
        def test(self, params: Params) -> None:
            if params.should_raise:
                with pytest.raises(ValueError, match="Invalid channel string"):
                    CharmChannel.parse(params.input)
            else:
                channel = CharmChannel.parse(params.input)
                assert channel.track == params.expected_track
                assert channel.risk == params.expected_risk
                assert channel.branch == params.expected_branch

    class TestStr:
        @dataclass
        class Params:
            label: str
            channel: CharmChannel
            expected: str

        test_cases = [
            Params(label="risk_only", channel=CharmChannel("", "stable", ""), expected="stable"),
            Params(label="track_and_risk", channel=CharmChannel("1.0", "stable", ""), expected="1.0/stable"),
            Params(
                label="track_risk_branch",
                channel=CharmChannel("1.0", "stable", "fix-123"),
                expected="1.0/stable/fix-123",
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
        def test(self, params: Params) -> None:
            assert str(params.channel) == params.expected

    class TestExplicitTrack:
        def test_returns_track_when_set(self) -> None:
            assert CharmChannel("1.0", "stable", "").explicit_track == "1.0"

        def test_returns_latest_when_empty(self) -> None:
            assert CharmChannel("", "stable", "").explicit_track == "latest"

    class TestOrdering:
        def test_stable_less_than_edge(self) -> None:
            assert CharmChannel("1.0", "stable", "") < CharmChannel("1.0", "edge", "")

        def test_earlier_track_less_than_later_track(self) -> None:
            assert CharmChannel("1.0", "stable", "") < CharmChannel("2.0", "stable", "")

        def test_equal_channels_not_less_than(self) -> None:
            assert not (CharmChannel("1.0", "stable", "") < CharmChannel("1.0", "stable", ""))


class TestJujuIntegrationApplication:
    def test_str_representation(self) -> None:
        # GIVEN an application endpoint
        endpoint = JujuIntegrationApplication(application="webapp", endpoint="database")

        # WHEN str is called
        result = str(endpoint)

        # THEN matches expected
        assert result == "webapp:database"

    class TestFromStr:
        @dataclass
        class Params:
            label: str
            input_str: str
            expected_application: str | None = None
            expected_endpoint: str | None = None
            should_raise: bool = False
            error_match: str | None = None

        test_cases = [
            Params(
                label="valid_simple",
                input_str="webapp:database",
                expected_application="webapp",
                expected_endpoint="database",
            ),
            Params(
                label="valid_with_colon_in_endpoint",
                input_str="webapp:db:special",
                expected_application="webapp",
                expected_endpoint="db:special",
            ),
            Params(
                label="invalid_no_colon",
                input_str="webapp",
                should_raise=True,
                error_match="Invalid JujuIntegrationApplication string",
            ),
            Params(
                label="invalid_empty_string",
                input_str="",
                should_raise=True,
                error_match="Invalid JujuIntegrationApplication string",
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            if params.should_raise:
                # WHEN / THEN parsing invalid string raises error
                with pytest.raises(ValueError, match=params.error_match if params.error_match else ""):
                    JujuIntegrationApplication.from_str(params.input_str)
            else:
                # WHEN parsing valid string
                endpoint = JujuIntegrationApplication.from_str(params.input_str)

                # THEN application and endpoint are correctly parsed
                assert endpoint.application == params.expected_application
                assert endpoint.endpoint == params.expected_endpoint
