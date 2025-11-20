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

from pathlib import Path

import pytest

from bundle_builder import CharmhubClient, OverridesClient


@pytest.fixture
def overrides_client() -> OverridesClient:
    static_charm_metadata_overrides = Path(__file__).parent / "../../../static/charm-metadata-overrides"
    return OverridesClient(charm_metadata_overrides=static_charm_metadata_overrides)


@pytest.fixture
def charmhub_client(overrides_client: OverridesClient) -> CharmhubClient:
    return CharmhubClient(overrides_client=overrides_client)


# Sample charm with only optional endpoints
@pytest.fixture
def sample_independent_charm() -> str:
    return "postgresql-k8s"


# Sample charm with one non-optional endpoint, fulfilled by the independent charm
@pytest.fixture
def sample_dependent_charm() -> str:
    return "kratos"


# Sample dependent endpoint of dependent charm
@pytest.fixture
def sample_dependent_charm_endpoint() -> str:
    return "pg-database"


# Sample fulfilling endpoint on independent charm
@pytest.fixture
def sample_independent_charm_endpoint() -> str:
    return "database"


@pytest.fixture
def sample_independent_charm_revision() -> int:
    return 495


@pytest.fixture
def sample_dependent_charm_revision() -> int:
    return 561


@pytest.fixture
def sample_arch() -> str:
    return "amd64"


@pytest.fixture
def sample_platform() -> str:
    return "kubernetes"
