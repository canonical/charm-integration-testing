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

"""Shared fixtures for overrides tests.

Overrides tests hit the real Charmhub API to cross-check that endpoint and
config keys declared in static override files match the charm's actual metadata.
They require network access and are expected to be slower than unit/logic tests.

Run with:
    ./scripts/bundle-builder-x-tests.sh overrides --overrides ./static/charm-overrides
"""

import warnings
from pathlib import Path

import pytest
import yaml

from bundle_builder_x.charm import CharmChannel
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.overrides import CharmGlobalOverrides, OverridesClient


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--overrides",
        required=True,
        help="Path to a directory of charm override YAML files to validate.",
    )
    parser.addoption(
        "--all-channels",
        action="store_true",
        default=False,
        help="Check every channel that matches an override's criteria instead of just one.",
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    overrides_dir = metafunc.config.getoption("--overrides")

    if "charm_name" in metafunc.fixturenames:
        if overrides_dir is None:
            metafunc.parametrize("charm_name", [])
        else:
            files = sorted(Path(overrides_dir).glob("*.yaml"))
            metafunc.parametrize("charm_name", [f.stem for f in files])

    if "charm_channel" not in metafunc.fixturenames:
        return
    if overrides_dir is None:
        metafunc.parametrize("charm_channel", [])
        return

    all_channels = bool(metafunc.config.getoption("--all-channels"))
    client = CharmhubClient()
    params: list[tuple[str, CharmChannel]] = []
    ids: list[str] = []
    for f in sorted(Path(overrides_dir).glob("*.yaml")):
        charm_name = f.stem
        try:
            global_overrides = CharmGlobalOverrides.model_validate(yaml.safe_load(f.read_text()))
        except Exception:
            continue  # YAML layer will catch this
        channels = client.get_charm_channels(charm_name)
        remaining = sorted(channels, key=lambda c: (c.track, c.risk))
        for override in global_overrides.overrides:
            matched = sorted([c for c in remaining if override.meets(c)], key=lambda c: (c.track, c.risk))
            remaining = [c for c in remaining if not override.meets(c)]
            if not matched:
                criteria_repr = [c.model_dump(exclude_none=True) for c in override.criteria]
                warnings.warn(f"{charm_name}: override with criteria={criteria_repr} matches no published channels")
                continue
            for channel in matched if all_channels else matched[:1]:
                params.append((charm_name, channel))
                ids.append(f"{charm_name}[{channel}]")

    metafunc.parametrize("charm_channel", params, ids=ids)


@pytest.fixture(scope="session")
def charmhub_client() -> CharmhubClient:
    return CharmhubClient()


@pytest.fixture(scope="session")
def overrides_client(request: pytest.FixtureRequest) -> OverridesClient:
    return OverridesClient(overrides=Path(request.config.getoption("--overrides")))


@pytest.fixture(scope="session")
def overrides_charmhub_client(charmhub_client: CharmhubClient, overrides_client: OverridesClient) -> CharmhubClient:
    return CharmhubClient(
        http_client=charmhub_client.http_client,
        overrides_client=overrides_client,
    )
