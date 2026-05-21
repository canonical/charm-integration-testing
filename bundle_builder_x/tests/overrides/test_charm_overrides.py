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

"""Charm override file validation tests.

Parametrized by (charm_name, channel): conftest computes the set of
(charm_name, channel) pairs covered by each override file at collection time,
applying first-met semantics so each channel maps to exactly one override block.

For each pair the test calls charm_from_store and expects no UnparsableCharmException,
which the production code raises when an override declares stale endpoint or config keys.
"""

import pytest
import yaml

from bundle_builder_x.charm import CharmChannel
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.charmhub_http import UnparsableCharmException
from bundle_builder_x.overrides import CharmGlobalOverrides, OverridesClient


def test_charm_override_yaml_is_valid(
    charm_name: str,
    overrides_client: OverridesClient,
) -> None:
    assert overrides_client.overrides is not None
    raw = yaml.safe_load((overrides_client.overrides / f"{charm_name}.yaml").read_text())
    CharmGlobalOverrides.model_validate(raw)


def test_charm_override_file_is_valid(
    charm_channel: tuple[str, CharmChannel],
    overrides_charmhub_client: CharmhubClient,
) -> None:
    charm_name, channel = charm_channel
    try:
        overrides_charmhub_client.charm_from_store(
            charm_name=charm_name,
            ubuntu_arch="amd64",
            charm_track=channel.track,
            charm_risk=channel.risk,
        )
    except UnparsableCharmException as exc:
        pytest.fail(str(exc))
