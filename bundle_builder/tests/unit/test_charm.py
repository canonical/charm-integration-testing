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
from pydantic import Field, TypeAdapter
from pydantic.dataclasses import dataclass

from bundle_builder.charm import (
    ENDPOINT_PROVIDES,
    ENDPOINT_REQUIRES,
    Charm,
    CharmChannel,
    CharmConfig,
    CharmConfigCriteria,
    CharmEndpoint,
    CharmEndpointOptionality,
    CharmLimit,
    CharmLimitCriteria,
    CharmTestConfig,
)


def channel_from_string(channel_str: str) -> CharmChannel:
    """Helper to create CharmChannel from string using Pydantic validation."""
    adapter = TypeAdapter(CharmChannel)
    return adapter.validate_python(channel_str)


def sample_charm_endpoint_postgresql_k8s_certificates() -> CharmEndpoint:
    return CharmEndpoint(
        type=ENDPOINT_REQUIRES,
        name="certificates",
        interface="tls-certificates",
        optionality=CharmEndpointOptionality.from_bool(True),
        limits=(),
    )


def sample_charm_endpoint_postgresql_k8s_database() -> CharmEndpoint:
    return CharmEndpoint(
        type=ENDPOINT_PROVIDES,
        name="database",
        interface="db",
        optionality=CharmEndpointOptionality.from_bool(True),
        limits=(),
    )


def sample_charm_postgresql_k8s() -> Charm:
    return Charm(
        name="postgresql-k8s",
        channel="stable",
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=frozenset(
            {
                sample_charm_endpoint_postgresql_k8s_certificates(),
                sample_charm_endpoint_postgresql_k8s_database(),
            }
        ),
        priority=2.0,
    )


def sample_charm_endpoint_pgbouncer_k8s_database() -> CharmEndpoint:
    return CharmEndpoint(
        type=ENDPOINT_PROVIDES,
        name="database",
        interface="db",
        optionality=CharmEndpointOptionality.from_bool(True),
        limits=(),
    )


def sample_charm_endpoint_pgbouncer_k8s_backend_database() -> CharmEndpoint:
    return CharmEndpoint(
        type=ENDPOINT_REQUIRES,
        name="backend-database",
        interface="db",
        optionality=CharmEndpointOptionality.from_bool(False),
        limits=(),
    )


def sample_charm_pgbouncer_k8s() -> Charm:
    return Charm(
        name="pgbouncer-k8s",
        channel="stable",
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=frozenset(
            {
                sample_charm_endpoint_pgbouncer_k8s_database(),
                sample_charm_endpoint_pgbouncer_k8s_backend_database(),
            }
        ),
        priority=2.0,
    )


def sample_charm_endpoint_kratos_pg_database() -> CharmEndpoint:
    return CharmEndpoint(
        type=ENDPOINT_REQUIRES,
        name="pg-database",
        interface="db",
        optionality=CharmEndpointOptionality.from_bool(False),
        limits=(),
    )


def sample_charm_kratos() -> Charm:
    return Charm(
        name="kratos",
        channel="edge",
        revision=123,
        ubuntu_version="24.04",
        ubuntu_arch="amd64",
        endpoints=frozenset(
            {
                sample_charm_endpoint_kratos_pg_database(),
            }
        ),
        priority=1.0,
    )


def sample_charm_endpoint_self_signed_certificates_certificates() -> CharmEndpoint:
    return CharmEndpoint(
        type=ENDPOINT_PROVIDES,
        name="certificates",
        interface="tls-certificates",
        optionality=CharmEndpointOptionality.from_bool(True),
        limits=(),
    )


def sample_charm_self_signed_certificates() -> Charm:
    return Charm(
        name="self-signed-certificates",
        channel="edge",
        revision=444,
        ubuntu_version="20.04",
        ubuntu_arch="amd64",
        endpoints=frozenset(
            {
                sample_charm_endpoint_self_signed_certificates_certificates(),
            }
        ),
        priority=4.0,
    )


class TestCharmEndpointOptionality:
    class TestIsOptional:
        @dataclass
        class Params:
            label: str
            optionality: CharmEndpointOptionality
            endpoints: set[str]
            is_optional: bool

        always_optional = CharmEndpointOptionality()

        optional_if_endpoint = CharmEndpointOptionality(endpoint_integrated="db")
        optional_if_all = CharmEndpointOptionality(all_of=frozenset({optional_if_endpoint}))
        optional_if_any = CharmEndpointOptionality(any_of=frozenset({optional_if_endpoint}))
        not_optional_if_in_none_of = CharmEndpointOptionality(none_of=frozenset({optional_if_endpoint}))

        test_cases = [
            Params(
                label="no_conditions_is_always_optional",
                optionality=always_optional,
                endpoints=set(),
                is_optional=True,
            ),
            Params(
                label="endpoint_integrated_true",
                optionality=optional_if_endpoint,
                endpoints={"db"},
                is_optional=True,
            ),
            Params(
                label="endpoint_integrated_false",
                optionality=optional_if_endpoint,
                endpoints={"api"},
                is_optional=False,
            ),
            Params(
                label="all_of_true",
                optionality=optional_if_all,
                endpoints={"db"},
                is_optional=True,
            ),
            Params(
                label="all_of_false",
                optionality=optional_if_all,
                endpoints={"api"},
                is_optional=False,
            ),
            Params(
                label="any_of_true",
                optionality=optional_if_any,
                endpoints={"db"},
                is_optional=True,
            ),
            Params(
                label="any_of_false",
                optionality=optional_if_any,
                endpoints={"api"},
                is_optional=False,
            ),
            Params(
                label="none_of_true",
                optionality=not_optional_if_in_none_of,
                endpoints={"api"},
                is_optional=True,
            ),
            Params(
                label="none_of_false",
                optionality=not_optional_if_in_none_of,
                endpoints={"db"},
                is_optional=False,
            ),
            Params(
                label="complex_all_true",
                optionality=CharmEndpointOptionality(
                    all_of=frozenset(
                        {
                            optional_if_endpoint,
                            CharmEndpointOptionality(endpoint_integrated="api"),
                        }
                    )
                ),
                endpoints={"db", "api"},
                is_optional=True,
            ),
            Params(
                label="complex_all_false",
                optionality=CharmEndpointOptionality(
                    all_of=frozenset(
                        {
                            optional_if_endpoint,
                            CharmEndpointOptionality(endpoint_integrated="other"),
                        }
                    )
                ),
                endpoints={"db", "api"},
                is_optional=False,
            ),
            Params(
                label="combined_all_conditions_true",
                optionality=CharmEndpointOptionality(
                    all_of=frozenset({optional_if_endpoint}),
                    any_of=frozenset({optional_if_endpoint}),
                    none_of=frozenset({CharmEndpointOptionality(endpoint_integrated="api")}),
                    endpoint_integrated="db",
                ),
                endpoints={"db"},
                is_optional=True,
            ),
            Params(
                label="combined_all_conditions_false_due_to_none_of",
                optionality=CharmEndpointOptionality(
                    all_of=frozenset({optional_if_endpoint}),
                    any_of=frozenset({optional_if_endpoint}),
                    none_of=frozenset({CharmEndpointOptionality(endpoint_integrated="db")}),
                    endpoint_integrated="db",
                ),
                endpoints={"db"},
                is_optional=False,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN the optionality
            optionality = params.optionality

            # WHEN is optional is called with the set of endpoints
            is_optional = optionality.is_optional(frozenset(params.endpoints))

            # THEN matches expected
            assert is_optional == params.is_optional

    class TestFromBool:
        @dataclass
        class Params:
            label: str
            value: bool
            endpoints: set[str] = Field(default_factory=set)

        test_cases = [
            Params(label="true_without_endpoints", value=True),
            Params(label="false_without_endpoints", value=False),
            Params(label="true_with_endpoints", value=True, endpoints={"endpoint_1", "endpoint_2"}),
            Params(label="false_with_endpoints", value=False, endpoints={"endpoint_1", "endpoint_2"}),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN the optionality for the value
            optionality = CharmEndpointOptionality.from_bool(params.value)

            # WHEN is optional is called
            is_optional = optionality.is_optional(frozenset(params.endpoints))

            # THEN matches expected
            assert is_optional == params.value


class TestCharmLimitCriteria:
    class TestValid:
        @dataclass
        class Params:
            label: str
            criteria: "CharmLimitCriteria"
            endpoints: frozenset[str]
            is_valid: bool

        criteria_endpoint_integrated = CharmLimitCriteria(endpoint_integrated="db")
        criteria_all_of = CharmLimitCriteria(all_of=frozenset({criteria_endpoint_integrated}))
        criteria_any_of = CharmLimitCriteria(any_of=frozenset({criteria_endpoint_integrated}))
        criteria_none_of = CharmLimitCriteria(none_of=frozenset({criteria_endpoint_integrated}))

        test_cases = [
            Params(
                label="no_conditions_is_always_valid",
                criteria=CharmLimitCriteria(),
                endpoints=frozenset(),
                is_valid=True,
            ),
            Params(
                label="endpoint_integrated_true",
                criteria=criteria_endpoint_integrated,
                endpoints=frozenset({"db"}),
                is_valid=True,
            ),
            Params(
                label="endpoint_integrated_false",
                criteria=criteria_endpoint_integrated,
                endpoints=frozenset({"api"}),
                is_valid=False,
            ),
            Params(
                label="all_of_true",
                criteria=criteria_all_of,
                endpoints=frozenset({"db"}),
                is_valid=True,
            ),
            Params(
                label="all_of_false",
                criteria=criteria_all_of,
                endpoints=frozenset({"api"}),
                is_valid=False,
            ),
            Params(
                label="any_of_true",
                criteria=criteria_any_of,
                endpoints=frozenset({"db"}),
                is_valid=True,
            ),
            Params(
                label="any_of_false",
                criteria=criteria_any_of,
                endpoints=frozenset({"api"}),
                is_valid=False,
            ),
            Params(
                label="none_of_true",
                criteria=criteria_none_of,
                endpoints=frozenset({"api"}),
                is_valid=True,
            ),
            Params(
                label="none_of_false",
                criteria=criteria_none_of,
                endpoints=frozenset({"db"}),
                is_valid=False,
            ),
            Params(
                label="complex_all_true",
                criteria=CharmLimitCriteria(
                    all_of=frozenset(
                        {
                            criteria_endpoint_integrated,
                            CharmLimitCriteria(endpoint_integrated="api"),
                        }
                    )
                ),
                endpoints=frozenset({"db", "api"}),
                is_valid=True,
            ),
            Params(
                label="complex_all_false",
                criteria=CharmLimitCriteria(
                    all_of=frozenset(
                        {
                            criteria_endpoint_integrated,
                            CharmLimitCriteria(endpoint_integrated="other"),
                        }
                    )
                ),
                endpoints=frozenset({"db", "api"}),
                is_valid=False,
            ),
            Params(
                label="combined_all_conditions_true",
                criteria=CharmLimitCriteria(
                    all_of=frozenset({criteria_endpoint_integrated}),
                    any_of=frozenset({criteria_endpoint_integrated}),
                    none_of=frozenset({CharmLimitCriteria(endpoint_integrated="api")}),
                    endpoint_integrated="db",
                ),
                endpoints=frozenset({"db"}),
                is_valid=True,
            ),
            Params(
                label="combined_all_conditions_false_due_to_none_of",
                criteria=CharmLimitCriteria(
                    all_of=frozenset({criteria_endpoint_integrated}),
                    any_of=frozenset({criteria_endpoint_integrated}),
                    none_of=frozenset({CharmLimitCriteria(endpoint_integrated="db")}),
                    endpoint_integrated="db",
                ),
                endpoints=frozenset({"db"}),
                is_valid=False,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN the criteria and endpoints
            criteria = params.criteria
            endpoints = params.endpoints

            # WHEN valid is called
            is_valid = criteria.valid(endpoints)

            # THEN matches expected
            assert is_valid == params.is_valid

    class TestFromBool:
        @dataclass
        class Params:
            label: str
            value: bool
            endpoints: frozenset[str] = Field(default_factory=frozenset)

        test_cases = [
            Params(label="true_without_endpoints", value=True),
            Params(label="false_without_endpoints", value=False),
            Params(label="true_with_endpoints", value=True, endpoints=frozenset({"endpoint_1", "endpoint_2"})),
            Params(label="false_with_endpoints", value=False, endpoints=frozenset({"endpoint_1", "endpoint_2"})),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN the criteria for the value
            criteria = CharmLimitCriteria.from_bool(params.value)

            # WHEN valid is called
            is_valid = criteria.valid(params.endpoints)

            # THEN matches expected
            assert is_valid == params.value


class TestCharmLimit:
    class TestCriteriaValidation:
        @dataclass
        class Params:
            label: str
            limit: "CharmLimit"
            endpoints: frozenset[str]
            expected_valid: bool

        test_cases = [
            Params(
                label="always_valid_criteria",
                limit=CharmLimit(criteria=CharmLimitCriteria.from_bool(True), limit=5),
                endpoints=frozenset(),
                expected_valid=True,
            ),
            Params(
                label="never_valid_criteria",
                limit=CharmLimit(criteria=CharmLimitCriteria.from_bool(False), limit=5),
                endpoints=frozenset(),
                expected_valid=False,
            ),
            Params(
                label="conditional_criteria_met",
                limit=CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"), limit=10),
                endpoints=frozenset({"grafana-cloud-config"}),
                expected_valid=True,
            ),
            Params(
                label="conditional_criteria_not_met",
                limit=CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"), limit=10),
                endpoints=frozenset({"send-remote-write"}),
                expected_valid=False,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN a limit with criteria
            limit = params.limit

            # WHEN checking if criteria is valid
            is_valid = limit.criteria.valid(params.endpoints)

            # THEN matches expected
            assert is_valid == params.expected_valid


class TestCharmEndpoint:
    class TestLimitMethod:
        @dataclass
        class Params:
            label: str
            endpoint: CharmEndpoint
            integrated_endpoints: frozenset[str]
            expected_limit: int | None

        test_cases = [
            Params(
                label="no_limits_returns_none",
                endpoint=CharmEndpoint(
                    type=ENDPOINT_PROVIDES,
                    name="database",
                    interface="postgresql",
                    optionality=CharmEndpointOptionality.from_bool(False),
                    limits=(),
                ),
                integrated_endpoints=frozenset(),
                expected_limit=None,
            ),
            Params(
                label="single_unconditional_limit",
                endpoint=CharmEndpoint(
                    type=ENDPOINT_PROVIDES,
                    name="database",
                    interface="postgresql",
                    optionality=CharmEndpointOptionality.from_bool(False),
                    limits=(CharmLimit(limit=5),),
                ),
                integrated_endpoints=frozenset(),
                expected_limit=5,
            ),
            Params(
                label="conditional_limit_criteria_met",
                endpoint=CharmEndpoint(
                    type=ENDPOINT_PROVIDES,
                    name="database",
                    interface="postgresql",
                    optionality=CharmEndpointOptionality.from_bool(False),
                    limits=(
                        CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"), limit=10),
                        CharmLimit(limit=1),
                    ),
                ),
                integrated_endpoints=frozenset({"grafana-cloud-config"}),
                expected_limit=10,
            ),
            Params(
                label="conditional_limit_criteria_not_met_fallback_to_default",
                endpoint=CharmEndpoint(
                    type=ENDPOINT_PROVIDES,
                    name="database",
                    interface="postgresql",
                    optionality=CharmEndpointOptionality.from_bool(False),
                    limits=(
                        CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"), limit=10),
                        CharmLimit(limit=1),
                    ),
                ),
                integrated_endpoints=frozenset({"send-remote-write"}),
                expected_limit=1,
            ),
            Params(
                label="multiple_conditional_limits_first_match_wins",
                endpoint=CharmEndpoint(
                    type=ENDPOINT_PROVIDES,
                    name="metrics",
                    interface="prometheus",
                    optionality=CharmEndpointOptionality.from_bool(False),
                    limits=(
                        CharmLimit(
                            criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"),
                            limit=100,
                        ),
                        CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="send-remote-write"), limit=50),
                        CharmLimit(limit=5),
                    ),
                ),
                integrated_endpoints=frozenset({"send-remote-write"}),
                expected_limit=50,
            ),
            Params(
                label="no_matching_criteria_returns_none",
                endpoint=CharmEndpoint(
                    type=ENDPOINT_PROVIDES,
                    name="metrics",
                    interface="prometheus",
                    optionality=CharmEndpointOptionality.from_bool(False),
                    limits=(
                        CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"), limit=100),
                        CharmLimit(criteria=CharmLimitCriteria(endpoint_integrated="send-remote-write"), limit=50),
                    ),
                ),
                integrated_endpoints=frozenset({"other-endpoint"}),
                expected_limit=None,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN an endpoint with limits
            endpoint = params.endpoint

            # WHEN getting the limit for specific integrated endpoints
            limit = endpoint.limit(params.integrated_endpoints)

            # THEN matches expected
            assert limit == params.expected_limit


class TestCharmChannel:
    class TestValidateFromString:
        @dataclass
        class Params:
            label: str
            input_value: str
            expected_track: str
            expected_risk: str
            expected_branch: str

        test_cases = [
            Params(
                label="risk_only",
                input_value="stable",
                expected_track="",
                expected_risk="stable",
                expected_branch="",
            ),
            Params(
                label="track_and_risk",
                input_value="1.0/stable",
                expected_track="1.0",
                expected_risk="stable",
                expected_branch="",
            ),
            Params(
                label="track_risk_and_branch",
                input_value="1.0/edge/feature",
                expected_track="1.0",
                expected_risk="edge",
                expected_branch="feature",
            ),
            Params(
                label="latest_stable",
                input_value="latest/stable",
                expected_track="latest",
                expected_risk="stable",
                expected_branch="",
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN a channel string
            channel_str = params.input_value

            # WHEN creating a CharmChannel from string
            channel = channel_from_string(channel_str)

            # THEN the channel is parsed correctly
            assert channel.track == params.expected_track
            assert channel.risk == params.expected_risk
            assert channel.branch == params.expected_branch

        def test_invalid_channel_raises_error(self) -> None:
            # GIVEN an invalid channel string with too many parts
            invalid_channel = "track/risk/branch/extra"

            # WHEN creating a CharmChannel from the invalid string
            # THEN it raises a ValueError
            with pytest.raises(ValueError, match="Invalid channel string: track/risk/branch/extra"):
                channel_from_string(invalid_channel)

    class TestSerialize:
        @dataclass
        class Params:
            label: str
            track: str
            risk: str
            branch: str
            expected_str: str

        test_cases = [
            Params(
                label="risk_only",
                track="",
                risk="stable",
                branch="",
                expected_str="stable",
            ),
            Params(
                label="track_and_risk",
                track="1.0",
                risk="stable",
                branch="",
                expected_str="1.0/stable",
            ),
            Params(
                label="track_risk_and_branch",
                track="1.0",
                risk="edge",
                branch="feature",
                expected_str="1.0/edge/feature",
            ),
            Params(
                label="all_fields",
                track="latest",
                risk="candidate",
                branch="",
                expected_str="latest/candidate",
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test_str(self, params: Params) -> None:
            # GIVEN a CharmChannel
            channel = CharmChannel(track=params.track, risk=params.risk, branch=params.branch)

            # WHEN converting to string
            channel_str = str(channel)

            # THEN the string representation is correct
            assert channel_str == params.expected_str

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test_serialize_model(self, params: Params) -> None:
            # GIVEN a CharmChannel
            channel = CharmChannel(track=params.track, risk=params.risk, branch=params.branch)

            # WHEN serializing the model
            serialized = channel.serialize_model()

            # THEN the serialized value is correct
            assert serialized == params.expected_str

    class TestExplicitTrack:
        @dataclass
        class Params:
            label: str
            track: str
            expected_explicit_track: str

        test_cases = [
            Params(
                label="empty_track_returns_latest",
                track="",
                expected_explicit_track="latest",
            ),
            Params(
                label="specific_track_returns_track",
                track="1.0",
                expected_explicit_track="1.0",
            ),
            Params(
                label="latest_track_returns_latest",
                track="latest",
                expected_explicit_track="latest",
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN a CharmChannel
            channel = CharmChannel(track=params.track, risk="stable", branch="")

            # WHEN accessing explicit_track
            explicit_track = channel.explicit_track

            # THEN the explicit track is correct
            assert explicit_track == params.expected_explicit_track


class TestCharmConfigCriteria:
    class TestValid:
        def test_no_conditions_is_always_valid(self) -> None:
            # GIVEN a criteria with no conditions
            criteria = CharmConfigCriteria()
            channel = channel_from_string("stable")

            # WHEN valid is called
            is_valid = criteria.valid(channel, frozenset())

            # THEN it's always valid
            assert is_valid is True

        def test_track_matches_explicit_track(self) -> None:
            # GIVEN a criteria for track 1.0
            criteria = CharmConfigCriteria(track="1.0")
            channel = channel_from_string("1.0/stable")

            # WHEN valid is called
            is_valid = criteria.valid(channel, frozenset())

            # THEN it's valid
            assert is_valid is True

        def test_track_does_not_match(self) -> None:
            # GIVEN a criteria for track 1.0
            criteria = CharmConfigCriteria(track="1.0")
            channel = channel_from_string("2.0/stable")

            # WHEN valid is called
            is_valid = criteria.valid(channel, frozenset())

            # THEN it's not valid
            assert is_valid is False

        def test_empty_track_matches_latest(self) -> None:
            # GIVEN a criteria for latest track
            criteria = CharmConfigCriteria(track="latest")
            channel = channel_from_string("stable")

            # WHEN valid is called
            is_valid = criteria.valid(channel, frozenset())

            # THEN it's valid (empty track = latest)
            assert is_valid is True

        def test_endpoint_integrated_true(self) -> None:
            # GIVEN a criteria requiring database endpoint
            criteria = CharmConfigCriteria(endpoint_integrated="db")
            channel = channel_from_string("stable")

            # WHEN valid is called with db endpoint integrated
            is_valid = criteria.valid(channel, frozenset({"db"}))

            # THEN it's valid
            assert is_valid is True

        def test_endpoint_integrated_false(self) -> None:
            # GIVEN a criteria requiring database endpoint
            criteria = CharmConfigCriteria(endpoint_integrated="db")
            channel = channel_from_string("stable")

            # WHEN valid is called without db endpoint
            is_valid = criteria.valid(channel, frozenset({"api"}))

            # THEN it's not valid
            assert is_valid is False

        def test_all_of_all_true(self) -> None:
            # GIVEN a criteria with all_of conditions
            criteria = CharmConfigCriteria(
                all_of=frozenset(
                    {
                        CharmConfigCriteria(track="1.0"),
                        CharmConfigCriteria(endpoint_integrated="db"),
                    }
                )
            )
            channel = channel_from_string("1.0/stable")

            # WHEN valid is called with all conditions true
            is_valid = criteria.valid(channel, frozenset({"db"}))

            # THEN it's valid
            assert is_valid is True

        def test_all_of_one_false(self) -> None:
            # GIVEN a criteria with all_of conditions
            criteria = CharmConfigCriteria(
                all_of=frozenset(
                    {
                        CharmConfigCriteria(track="1.0"),
                        CharmConfigCriteria(endpoint_integrated="db"),
                    }
                )
            )
            channel = channel_from_string("1.0/stable")

            # WHEN valid is called with one condition false
            is_valid = criteria.valid(channel, frozenset({"api"}))

            # THEN it's not valid
            assert is_valid is False

        def test_any_of_one_true(self) -> None:
            # GIVEN a criteria with any_of conditions
            criteria = CharmConfigCriteria(
                any_of=frozenset(
                    {
                        CharmConfigCriteria(track="1.0"),
                        CharmConfigCriteria(endpoint_integrated="db"),
                    }
                )
            )
            channel = channel_from_string("2.0/stable")

            # WHEN valid is called with one condition true
            is_valid = criteria.valid(channel, frozenset({"db"}))

            # THEN it's valid
            assert is_valid is True

        def test_any_of_all_false(self) -> None:
            # GIVEN a criteria with any_of conditions
            criteria = CharmConfigCriteria(
                any_of=frozenset(
                    {
                        CharmConfigCriteria(track="1.0"),
                        CharmConfigCriteria(endpoint_integrated="db"),
                    }
                )
            )
            channel = channel_from_string("2.0/stable")

            # WHEN valid is called with all conditions false
            is_valid = criteria.valid(channel, frozenset({"api"}))

            # THEN it's not valid
            assert is_valid is False

        def test_none_of_condition_not_met(self) -> None:
            # GIVEN a criteria with none_of conditions
            criteria = CharmConfigCriteria(none_of=frozenset({CharmConfigCriteria(endpoint_integrated="db")}))
            channel = channel_from_string("stable")

            # WHEN valid is called with condition not met
            is_valid = criteria.valid(channel, frozenset({"api"}))

            # THEN it's valid
            assert is_valid is True

        def test_none_of_condition_met(self) -> None:
            # GIVEN a criteria with none_of conditions
            criteria = CharmConfigCriteria(none_of=frozenset({CharmConfigCriteria(endpoint_integrated="db")}))
            channel = channel_from_string("stable")

            # WHEN valid is called with condition met
            is_valid = criteria.valid(channel, frozenset({"db"}))

            # THEN it's not valid
            assert is_valid is False

        def test_complex_combined_all_true(self) -> None:
            # GIVEN a criteria with all condition types
            criteria = CharmConfigCriteria(
                all_of=frozenset({CharmConfigCriteria(track="1.0")}),
                any_of=frozenset({CharmConfigCriteria(endpoint_integrated="db")}),
                none_of=frozenset({CharmConfigCriteria(endpoint_integrated="api")}),
                endpoint_integrated="db",
            )
            channel = channel_from_string("1.0/stable")

            # WHEN valid is called with all conditions true
            is_valid = criteria.valid(channel, frozenset({"db"}))

            # THEN it's valid
            assert is_valid is True

        def test_complex_combined_none_of_fails(self) -> None:
            # GIVEN a criteria with all condition types including failing none_of
            criteria = CharmConfigCriteria(
                all_of=frozenset({CharmConfigCriteria(track="1.0")}),
                any_of=frozenset({CharmConfigCriteria(endpoint_integrated="db")}),
                none_of=frozenset({CharmConfigCriteria(endpoint_integrated="db")}),
                endpoint_integrated="db",
            )
            channel = channel_from_string("1.0/stable")

            # WHEN valid is called with none_of condition failing
            is_valid = criteria.valid(channel, frozenset({"db"}))

            # THEN it's not valid
            assert is_valid is False

    class TestFromBool:
        @dataclass
        class Params:
            label: str
            value: bool
            channel_str: str = "stable"
            endpoints: frozenset[str] = Field(default_factory=frozenset)

        test_cases = [
            Params(label="true_is_always_valid", value=True),
            Params(label="false_is_never_valid", value=False),
            Params(
                label="true_with_channel_and_endpoints",
                value=True,
                channel_str="1.0/stable",
                endpoints=frozenset({"db", "api"}),
            ),
            Params(
                label="false_with_channel_and_endpoints",
                value=False,
                channel_str="1.0/stable",
                endpoints=frozenset({"db", "api"}),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN a criteria from bool and channel
            criteria = CharmConfigCriteria.from_bool(params.value)
            channel = channel_from_string(params.channel_str)

            # WHEN valid is called
            is_valid = criteria.valid(channel, params.endpoints)

            # THEN it matches the expected value
            assert is_valid == params.value

    class TestValidateConfigFromDict:
        def test_list_converts_to_all_of(self) -> None:
            # GIVEN a list of criteria
            criteria_list = [
                {"track": "1.0"},
                {"endpoint_integrated": "db"},
            ]

            # WHEN creating CharmConfigCriteria from the list
            criteria = CharmConfigCriteria(criteria_list)

            # THEN it's converted to all_of
            assert criteria.all_of is not None
            assert len(criteria.all_of) == 2
            assert any(c.track == "1.0" for c in criteria.all_of)
            assert any(c.endpoint_integrated == "db" for c in criteria.all_of)

        def test_dict_remains_dict(self) -> None:
            # GIVEN a dict of criteria
            criteria_dict = {"track": "1.0", "endpoint_integrated": "db"}

            # WHEN creating CharmConfigCriteria from the dict
            criteria = CharmConfigCriteria(**criteria_dict)

            # THEN it's created with the dict values
            assert criteria.track == "1.0"
            assert criteria.endpoint_integrated == "db"


class TestCharmTestConfig:
    class TestValidateConfigFromDict:
        @dataclass
        class Params:
            label: str
            input_value: dict[str, dict[str, Any]]
            expected_config: CharmConfig

        test_cases = [
            Params(
                label="dict_config_converts_to_tuple",
                input_value={"config": {"key1": "value1", "key2": 123}},
                expected_config=(("key1", "value1"), ("key2", 123)),
            ),
            Params(
                label="empty_dict_config",
                input_value={"config": {}},
                expected_config=(),
            ),
            Params(
                label="single_item_config",
                input_value={"config": {"option": "enabled"}},
                expected_config=(("option", "enabled"),),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN a dict with config
            input_data = params.input_value

            # WHEN creating CharmTestConfig using TypeAdapter for proper validation
            adapter = TypeAdapter(CharmTestConfig)
            test_config = adapter.validate_python(input_data)

            # THEN the config is converted to tuple format
            assert test_config.config == params.expected_config

    def test_default_criteria_is_always_valid(self) -> None:
        # GIVEN a CharmTestConfig with default criteria
        test_config = CharmTestConfig(config=(("key", "value"),))
        channel = channel_from_string("stable")

        # WHEN checking if criteria is valid
        is_valid = test_config.criteria.valid(channel, frozenset())

        # THEN it's always valid
        assert is_valid is True

    def test_config_with_criteria(self) -> None:
        # GIVEN a CharmTestConfig with specific criteria
        criteria = CharmConfigCriteria(track="1.0")
        test_config = CharmTestConfig(criteria=criteria, config=(("key", "value"),))
        channel_match = channel_from_string("1.0/stable")
        channel_no_match = channel_from_string("2.0/stable")

        # WHEN checking validity for matching channel
        is_valid_match = test_config.criteria.valid(channel_match, frozenset())
        # AND checking validity for non-matching channel
        is_valid_no_match = test_config.criteria.valid(channel_no_match, frozenset())

        # THEN it validates correctly
        assert is_valid_match is True
        assert is_valid_no_match is False


class TestCharm:
    def test_repr(self) -> None:
        # GIVEN a charm
        charm = sample_charm_postgresql_k8s()

        # WHEN repr is called
        # TODO(raul): remove type ignore in subsequent type checker PRs
        repr = charm.__repr__()  # type: ignore

        # THEN repr is charm name
        assert repr == charm.name
