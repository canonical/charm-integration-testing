# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import Any, cast

import pytest
import requests
from pydantic import ValidationError

from bundle_builder_x.charmhub_http import (
    CHARMHUB_API_URL_ENV,
    DEFAULT_CHARMHUB_API_URL,
    CharmhubHttpClient,
    InfoResponse,
)


def test_info_requests_and_parses_default_channel() -> None:
    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {"default-release": {"channel": {"name": "1.31/candidate"}}}

    class Session:
        def mount(self, *args: Any) -> None:
            pass

        def get(self, **kwargs: Any) -> Response:
            assert "default-release.channel" in kwargs["params"]["fields"].split(",")
            return Response()

    response = CharmhubHttpClient(session=cast(requests.Session, Session())).info("istio-k8s")
    assert response.default_release.channel is not None
    assert response.default_release.channel.name == "1.31/candidate"
    with pytest.raises(ValidationError, match="frozen"):
        response.default_release.channel.name = "1/stable"  # type: ignore[misc]  # Verify runtime immutability.


@pytest.mark.parametrize("base_fields", [{}, {"base": None}], ids=["missing", "null"])
def test_channel_map_requires_base(base_fields: dict[str, None]) -> None:
    # GIVEN a channel-map entry with a missing or null base
    channel = {"name": "stable", "track": "latest", "risk": "stable", **base_fields}

    # WHEN parsing the response, THEN invalid base metadata is rejected at the boundary
    with pytest.raises(ValidationError, match="channel-map.0.channel.base"):
        InfoResponse.model_validate({"channel-map": [{"channel": channel}]})


class TestCharmhubHttpClient:
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
