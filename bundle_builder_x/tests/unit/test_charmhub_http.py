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

from bundle_builder_x.charmhub_http import (
    CHARMHUB_API_URL_ENV,
    DEFAULT_CHARMHUB_API_URL,
    CharmhubHttpClient,
)


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
