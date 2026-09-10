# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass

import pytest
from juju import CharmChannel, JujuConsumedOfferInfo, JujuIntegrationApplication, JujuModelHandle


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


class TestJujuConsumedOfferInfo:
    class TestParseUrl:
        @dataclass
        class Params:
            label: str
            url: str
            expected_owner: str | None = None
            expected_model: JujuModelHandle | None = None
            expected_offer_name: str | None = None
            should_be_none: bool = False

        test_cases = [
            Params(
                label="valid_url",
                url="other-controller:admin/other-model.postgresql-k8s",
                expected_owner="admin",
                expected_model=JujuModelHandle(controller="other-controller", model="other-model"),
                expected_offer_name="postgresql-k8s",
            ),
            Params(
                label="valid_url_non_admin_owner",
                url="my-controller:alice/my-model.mysql-offer",
                expected_owner="alice",
                expected_model=JujuModelHandle(controller="my-controller", model="my-model"),
                expected_offer_name="mysql-offer",
            ),
            Params(label="missing_colon", url="admin/other-model.postgresql-k8s", should_be_none=True),
            Params(label="missing_slash", url="other-controller:other-model.postgresql-k8s", should_be_none=True),
            Params(
                label="slash_before_colon_only",
                url="other/controller:other-model.postgresql-k8s",
                should_be_none=True,
            ),
            Params(label="missing_dot", url="other-controller:admin/other-model-postgresql-k8s", should_be_none=True),
            Params(label="empty_string", url="", should_be_none=True),
            Params(label="empty_controller", url=":admin/other-model.postgresql-k8s", should_be_none=True),
            Params(label="empty_owner", url="other-controller:/other-model.postgresql-k8s", should_be_none=True),
            Params(label="empty_model", url="other-controller:admin/.postgresql-k8s", should_be_none=True),
            Params(label="empty_offer_name", url="other-controller:admin/other-model.", should_be_none=True),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=lambda p: p.label)
        def test_parse_url(self, params: "TestJujuConsumedOfferInfo.TestParseUrl.Params") -> None:
            # GIVEN a consumed offer with a given URL
            offer = JujuConsumedOfferInfo(url=params.url)

            # WHEN parsing the URL
            result = offer.parse_url()

            # THEN the owner, offering model, and offer name are correctly parsed (or None for
            # malformed URLs)
            if params.should_be_none:
                assert result is None
            else:
                assert result == (params.expected_owner, params.expected_model, params.expected_offer_name)
