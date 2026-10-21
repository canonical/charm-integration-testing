# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from pydantic import ValidationError
from requests import Response

from bundle_builder_x.charmhub_http import (
    CHARMHUB_API_URL_ENV,
    DEFAULT_CHARMHUB_API_URL,
    CharmhubHttpClient,
    InfoResponse,
)


@pytest.mark.parametrize("base_fields", [{}, {"base": None}], ids=["missing", "null"])
def test_channel_map_requires_base(base_fields: dict[str, None]) -> None:
    # GIVEN a channel-map entry with a missing or null base
    channel = {"name": "stable", "track": "latest", "risk": "stable", **base_fields}

    # WHEN parsing the response, THEN invalid base metadata is rejected at the boundary
    with pytest.raises(ValidationError, match="channel-map.0.channel.base"):
        InfoResponse.model_validate({"channel-map": [{"channel": channel}]})


class TestCharmhubHttpClient:
    @pytest.mark.parametrize("include_channel_map", [False, True])
    def test_info_requests_default_channel(self, monkeypatch: pytest.MonkeyPatch, include_channel_map: bool) -> None:
        # GIVEN a transport that records the requested fields and returns a default channel
        client = CharmhubHttpClient()
        requested_fields: set[str] = set()

        def get(url: str, params: dict[str, str], headers: dict[str, str], timeout: float) -> Response:
            requested_fields.update(params["fields"].split(","))
            response = Response()
            response.status_code = 200
            response._content = b'{"default-release": {"channel": {"name": "1.31/candidate"}}}'
            return response

        monkeypatch.setattr(client.session, "get", get)

        # WHEN fetching charm information through the real HTTP client method
        info = client.info("istio-k8s", include_channel_map=include_channel_map)

        # THEN the default channel is requested and parsed, with the optional channel map preserved
        assert "default-release.channel" in requested_fields
        assert ("channel-map" in requested_fields) == include_channel_map
        assert info.default_release.channel is not None
        assert info.default_release.channel.name == "1.31/candidate"

    class TestBaseUrlResolution:
        def test_uses_hardcoded_default_when_no_arg_and_no_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
            # GIVEN no base_url argument and no env var
            monkeypatch.delenv(CHARMHUB_API_URL_ENV, raising=False)

            # WHEN the client is constructed
            client = CharmhubHttpClient()

            # THEN the hardcoded default is used
            assert client._refresh_endpoint == DEFAULT_CHARMHUB_API_URL + "/v2/charms/refresh"

        def test_uses_env_var_when_no_arg_provided(self, monkeypatch: pytest.MonkeyPatch) -> None:
            # GIVEN no base_url argument but an env var is set
            custom_url = "https://staging.charmhub.io"
            monkeypatch.setenv(CHARMHUB_API_URL_ENV, custom_url)

            # WHEN the client is constructed
            client = CharmhubHttpClient()

            # THEN the env var value is used
            assert client._refresh_endpoint == custom_url + "/v2/charms/refresh"

        def test_explicit_arg_takes_priority_over_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
            # GIVEN an explicit base_url argument and an env var both set
            explicit_url = "https://explicit.charmhub.io"
            monkeypatch.setenv(CHARMHUB_API_URL_ENV, "https://env.charmhub.io")

            # WHEN the client is constructed with the explicit argument
            client = CharmhubHttpClient(base_url=explicit_url)

            # THEN the explicit argument takes priority
            assert client._refresh_endpoint == explicit_url + "/v2/charms/refresh"
