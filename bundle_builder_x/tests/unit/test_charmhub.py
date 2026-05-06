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
            client = _client({"overrides": [{"requires": {"logging": {"optional": True}}}]})
            with pytest.raises(UnparsableCharmException, match="requires"):
                client._get_charm_endpoints("mycharm", _METADATA_REQUIRES, _CHANNEL)

        def test_valid_requires_key_passes(self) -> None:
            client = _client({"overrides": [{"requires": {"db": {"optional": True}}}]})
            endpoints = client._get_charm_endpoints("mycharm", _METADATA_REQUIRES, _CHANNEL)
            assert "db" in endpoints

        def test_stale_provides_key_raises(self) -> None:
            client = _client({"overrides": [{"provides": {"grafana-dashboard": {}}}]})
            with pytest.raises(UnparsableCharmException, match="provides"):
                client._get_charm_endpoints("mycharm", _METADATA_PROVIDES, _CHANNEL)

        def test_valid_provides_key_passes(self) -> None:
            client = _client({"overrides": [{"provides": {"web": {"optional": True}}}]})
            endpoints = client._get_charm_endpoints("mycharm", _METADATA_PROVIDES, _CHANNEL)
            assert "web" in endpoints

        def test_no_override_returns_metadata_endpoints(self) -> None:
            client = _client({})
            endpoints = client._get_charm_endpoints("mycharm", _METADATA_REQUIRES, _CHANNEL)
            assert "db" in endpoints

    class TestGetCharmConfigs:
        def test_stale_config_key_raises(self) -> None:
            client = _client({"overrides": [{"configs": {"gone-option": ["value"]}}]})
            with pytest.raises(UnparsableCharmException, match="config"):
                client._get_charm_configs("mycharm", _CHANNEL, _EMPTY_CONFIG)

        def test_valid_config_key_passes(self) -> None:
            schema = CharmConfigSchema(options={"my-option": CharmConfigSchema.Option(type="string")})
            client = _client({"overrides": [{"configs": {"my-option": ["v1"]}}]})
            result = client._get_charm_configs("mycharm", _CHANNEL, schema)
            assert result == {"my-option": ["v1"]}

        def test_no_config_override_returns_empty(self) -> None:
            client = _client({})
            assert client._get_charm_configs("mycharm", _CHANNEL, _EMPTY_CONFIG) == {}
