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

from bundle_builder.charm import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, Charm, CharmEndpoint, CharmEndpointOptionality


def sample_charm_endpoint_postgresql_k8s_certificates() -> CharmEndpoint:
    return CharmEndpoint(
        type=ENDPOINT_REQUIRES,
        name="certificates",
        interface="tls-certificates",
        optionality=CharmEndpointOptionality.from_bool(True),
        limit=None,
    )


def sample_charm_endpoint_postgresql_k8s_database() -> CharmEndpoint:
    return CharmEndpoint(
        type=ENDPOINT_PROVIDES,
        name="database",
        interface="db",
        optionality=CharmEndpointOptionality.from_bool(True),
        limit=None,
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


def sample_charm_endpoint_kratos_pg_database() -> CharmEndpoint:
    return CharmEndpoint(
        type=ENDPOINT_REQUIRES,
        name="pg-database",
        interface="db",
        optionality=CharmEndpointOptionality.from_bool(False),
        limit=None,
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
        limit=None,
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
        def test(self, params: Params):
            # GIVEN the optionality
            optionality = params.optionality

            # WHEN is optional is called with the set of endpoints
            is_optional = optionality.is_optional(params.endpoints)

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
        def test(self, params: Params):
            # GIVEN the optionality for the value
            optionality = CharmEndpointOptionality.from_bool(params.value)

            # WHEN is optional is called
            is_optional = optionality.is_optional(params.endpoints)

            # THEN matches expected
            assert is_optional == params.value


class TestCharm:
    def test_repr(self):
        # GIVEN a charm
        charm = sample_charm_postgresql_k8s()

        # WHEN repr is called
        repr = charm.__repr__()

        # THEN repr is charm name
        assert repr == charm.name
