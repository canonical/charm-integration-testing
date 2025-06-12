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

from bundle_builder import CharmhubClient


@pytest.fixture
def charmhub_client() -> CharmhubClient:
    return CharmhubClient()


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
def sample_arch() -> str:
    return "amd64"


@pytest.fixture
def sample_platform() -> str:
    return "kubernetes"
