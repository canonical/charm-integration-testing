# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest

from bundle_builder_x.snapstore_http import (
    DEFAULT_SNAPCRAFT_API_URL,
    SNAPCRAFT_API_URL_ENV,
    SnapstoreHttpClient,
)


class TestSnapstoreHttpClient:
    class TestBaseUrlResolution:
        def test_uses_hardcoded_default_when_no_arg_and_no_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
            # GIVEN no base_url argument and no env var
            monkeypatch.delenv(SNAPCRAFT_API_URL_ENV, raising=False)

            # WHEN the client is constructed
            client = SnapstoreHttpClient()

            # THEN the hardcoded default is used
            assert client._info_endpoint == DEFAULT_SNAPCRAFT_API_URL + "/v2/snaps/info/{snap_name}"

        def test_uses_env_var_when_no_arg_provided(self, monkeypatch: pytest.MonkeyPatch) -> None:
            # GIVEN no base_url argument but an env var is set
            custom_url = "https://staging.snapcraft.io"
            monkeypatch.setenv(SNAPCRAFT_API_URL_ENV, custom_url)

            # WHEN the client is constructed
            client = SnapstoreHttpClient()

            # THEN the env var value is used
            assert client._info_endpoint == custom_url + "/v2/snaps/info/{snap_name}"

        def test_explicit_arg_takes_priority_over_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
            # GIVEN an explicit base_url argument and an env var both set
            explicit_url = "https://explicit.snapcraft.io"
            monkeypatch.setenv(SNAPCRAFT_API_URL_ENV, "https://env.snapcraft.io")

            # WHEN the client is constructed with the explicit argument
            client = SnapstoreHttpClient(base_url=explicit_url)

            # THEN the explicit argument takes priority
            assert client._info_endpoint == explicit_url + "/v2/snaps/info/{snap_name}"
