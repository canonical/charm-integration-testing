# Copyright (C) 2026 Canonical Ltd

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

from bundle_builder_x.charm import CharmChannel
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.charmhub_http import CharmConfigSchema, CharmMetadata, UnparsableCharmException
from bundle_builder_x.overrides import CharmGlobalOverrides, OverridesClient

_CHANNEL = CharmChannel(track="latest", risk="stable", branch="")
_METADATA_REQUIRES = CharmMetadata(requires={"db": CharmMetadata.Endpoint(interface="pgsql")})
_METADATA_PROVIDES = CharmMetadata(provides={"web": CharmMetadata.Endpoint(interface="http")})
_EMPTY_CONFIG = CharmConfigSchema()


class _StubOverridesClient(OverridesClient):
    """OverridesClient that returns a fixed CharmGlobalOverrides for any charm."""

    def __init__(self, raw: dict[str, object]) -> None:
        super().__init__()
        self._overrides = CharmGlobalOverrides(**raw)

    def _get_charm_global_overrides(self, charm: str) -> CharmGlobalOverrides:  # type: ignore[override]
        return self._overrides


def _client(raw: dict[str, object]) -> CharmhubClient:
    return CharmhubClient(overrides_client=_StubOverridesClient(raw))


class TestCharmhubClient:
    class TestGetCharmEndpoints:
        def test_stale_requires_key_raises(self) -> None:
            # GIVEN an override declaring a requires endpoint absent from charm metadata
            client = _client({"overrides": [{"requires": {"logging": {"optional": True}}}]})
            # WHEN building endpoints
            # THEN UnparsableCharmException is raised mentioning "requires"
            with pytest.raises(UnparsableCharmException, match="requires"):
                client._get_charm_endpoints("mycharm", _METADATA_REQUIRES, _CHANNEL)

        def test_valid_requires_key_passes(self) -> None:
            # GIVEN an override for a requires endpoint that exists in metadata
            client = _client({"overrides": [{"requires": {"db": {"optional": True}}}]})
            # WHEN building endpoints
            endpoints = client._get_charm_endpoints("mycharm", _METADATA_REQUIRES, _CHANNEL)
            # THEN the endpoint is present
            assert "db" in endpoints

        def test_stale_provides_key_raises(self) -> None:
            # GIVEN an override declaring a provides endpoint absent from charm metadata
            client = _client({"overrides": [{"provides": {"grafana-dashboard": {}}}]})
            # WHEN building endpoints
            # THEN UnparsableCharmException is raised mentioning "provides"
            with pytest.raises(UnparsableCharmException, match="provides"):
                client._get_charm_endpoints("mycharm", _METADATA_PROVIDES, _CHANNEL)

        def test_valid_provides_key_passes(self) -> None:
            # GIVEN an override for a provides endpoint that exists in metadata
            client = _client({"overrides": [{"provides": {"web": {"optional": True}}}]})
            # WHEN building endpoints
            endpoints = client._get_charm_endpoints("mycharm", _METADATA_PROVIDES, _CHANNEL)
            # THEN the endpoint is present
            assert "web" in endpoints

        def test_no_override_returns_metadata_endpoints(self) -> None:
            # GIVEN no overrides
            client = _client({})
            # WHEN building endpoints
            endpoints = client._get_charm_endpoints("mycharm", _METADATA_REQUIRES, _CHANNEL)
            # THEN all metadata endpoints are returned
            assert "db" in endpoints

    class TestGetCharmConfigs:
        def test_stale_config_key_raises(self) -> None:
            # GIVEN an override declaring a config key absent from the charm's config schema
            client = _client({"overrides": [{"configs": {"gone-option": ["value"]}}]})
            # WHEN building configs
            # THEN UnparsableCharmException is raised mentioning "config"
            with pytest.raises(UnparsableCharmException, match="config"):
                client._get_charm_configs("mycharm", _CHANNEL, _EMPTY_CONFIG)

        def test_valid_config_key_passes(self) -> None:
            # GIVEN an override for a config key that exists in the schema
            schema = CharmConfigSchema(options={"my-option": CharmConfigSchema.Option(type="string")})
            client = _client({"overrides": [{"configs": {"my-option": ["v1"]}}]})
            # WHEN building configs
            result = client._get_charm_configs("mycharm", _CHANNEL, schema)
            # THEN the override value is returned
            assert result == {"my-option": ["v1"]}

        def test_no_config_override_returns_empty(self) -> None:
            # GIVEN no config overrides
            client = _client({})
            # WHEN building configs
            # THEN an empty dict is returned
            assert client._get_charm_configs("mycharm", _CHANNEL, _EMPTY_CONFIG) == {}
