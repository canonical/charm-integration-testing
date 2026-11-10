# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for overrides tests.

Overrides tests hit the real Charmhub API to cross-check that endpoint and
config keys declared in static override files match the charm's actual metadata.
They require network access and are expected to be slower than unit/logic tests.

Run with:
    ./scripts/bundle-builder-x-tests.sh overrides --overrides ./static/charm-overrides
"""

import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from bundle_builder_x.charm import CharmChannel
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.charmhub_http import InfoResponse
from bundle_builder_x.overrides import CharmGlobalOverrides, OverridesClient


def _override_channel_bases(
    info: InfoResponse,
    overrides: CharmGlobalOverrides,
    all_channels: bool,
) -> list[tuple[CharmChannel, str]]:
    """Select published amd64 Ubuntu releases using first-match override semantics."""
    remaining: set[tuple[CharmChannel, str]] = set()
    for entry in info.channel_map:
        base = entry.channel.base
        if base.name == "ubuntu" and base.architecture in {"amd64", "all"}:
            remaining.add((CharmChannel.model_validate(entry.channel.name), base.channel))

    selected: list[tuple[CharmChannel, str]] = []
    for override in overrides.overrides:
        matched = sorted(cv for cv in remaining if override.meets(*cv))
        remaining.difference_update(matched)
        if not matched:
            criteria = [c.model_dump(exclude_none=True) for c in override.criteria]
            raise ValueError(f"override with criteria={criteria} matches no published amd64 Ubuntu (channel, base)")
        selected.extend(matched if all_channels else matched[:1])
    return selected


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--overrides",
        required=False,
        default=None,
        help="Path to a directory of charm override YAML files to validate.",
    )
    parser.addoption(
        "--overrides-modified-since",
        required=False,
        default=None,
        help="Git ref (e.g. origin/main). When provided, only override files modified since that ref are tested.",
    )
    parser.addoption(
        "--all-channels",
        action="store_true",
        default=False,
        help="Check every published amd64 Ubuntu channel/base pair matching each override instead of just one.",
    )


def _get_override_files(overrides_dir: str, modified_since: str | None) -> list[Path]:
    all_files = sorted(Path(overrides_dir).glob("*.yaml"))
    if modified_since is None:
        return all_files
    output = subprocess.check_output(
        ["git", "diff", "--name-only", f"{modified_since}...HEAD", "--", overrides_dir],
        text=True,
    )
    changed = {Path(line).name for line in output.splitlines() if line}
    return [f for f in all_files if f.name in changed]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    overrides_dir = metafunc.config.getoption("--overrides")
    modified_since = metafunc.config.getoption("--overrides-modified-since")

    if "charm_name" in metafunc.fixturenames:
        if overrides_dir is None:
            metafunc.parametrize("charm_name", [])
        else:
            files = _get_override_files(overrides_dir, modified_since)
            metafunc.parametrize("charm_name", [f.stem for f in files])

    if "charm_channel" not in metafunc.fixturenames:
        return
    if overrides_dir is None:
        metafunc.parametrize("charm_channel", [])
        return

    all_channels = bool(metafunc.config.getoption("--all-channels"))
    client = CharmhubClient()
    params: list[tuple[str, CharmChannel, str]] = []
    ids: list[str] = []
    unmatched: list[str] = []
    for f in _get_override_files(overrides_dir, modified_since):
        charm_name = f.stem
        try:
            global_overrides = CharmGlobalOverrides.model_validate(yaml.safe_load(f.read_text()))
        except (yaml.YAMLError, ValidationError):
            continue  # YAML layer will catch this

        info = client.http_client.info(charm_name, include_channel_map=True)
        try:
            channel_bases = _override_channel_bases(info, global_overrides, all_channels)
        except ValueError as exc:
            unmatched.append(f"{charm_name}: {exc}")
            continue
        for channel, ubuntu_version in channel_bases:
            params.append((charm_name, channel, ubuntu_version))
            ids.append(f"{charm_name}[{channel}][{ubuntu_version}]")

    if unmatched:
        pytest.fail(
            "One or more overrides match no published (channel, ubuntu_version) combination:\n"
            + "\n".join(f"  - {m}" for m in unmatched)
        )

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
