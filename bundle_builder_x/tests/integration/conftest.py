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

"""Shared fixtures for integration tests.

Integration tests hit the real Charmhub and Snapstore APIs, so they
require network access and are expected to be slower than unit/logic tests.
"""

from pathlib import Path

import pytest

from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.overrides import OverridesClient
from bundle_builder_x.snapstore import SnapstoreClient


@pytest.fixture
def overrides_path() -> Path:
    return Path(__file__).parent / "../../../static/charm-overrides"


@pytest.fixture
def overrides_client(overrides_path: Path) -> OverridesClient:
    return OverridesClient(overrides=overrides_path)


@pytest.fixture
def charmhub_client(overrides_client: OverridesClient) -> CharmhubClient:
    return CharmhubClient(overrides_client=overrides_client)


@pytest.fixture
def snapstore_client() -> SnapstoreClient:
    return SnapstoreClient()
