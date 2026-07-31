# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

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
