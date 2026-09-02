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
from bundle_builder_x.overrides import CharmGlobalOverrides, CharmOverridesCriteria, OverridesClient


def _referenced_ubuntu_versions(criteria: CharmOverridesCriteria) -> set[str]:
    """Recursively collect every explicit ubuntu_version referenced by a criteria block."""
    versions = {criteria.ubuntu_version} if criteria.ubuntu_version else set()
    for group in (criteria.all_of, criteria.any_of, criteria.none_of):
        for nested in group or []:
            versions |= _referenced_ubuntu_versions(nested)
    return versions


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
        help="Check every channel that matches an override's criteria instead of just one.",
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
    params: list[tuple[str, CharmChannel, str | None]] = []
    ids: list[str] = []
    unmatched: list[str] = []
    for f in _get_override_files(overrides_dir, modified_since):
        charm_name = f.stem
        try:
            global_overrides = CharmGlobalOverrides.model_validate(yaml.safe_load(f.read_text()))
        except (yaml.YAMLError, ValidationError):
            continue  # YAML layer will catch this

        # Some overrides only apply to a specific Ubuntu base (e.g. kubernetes-worker's
        # legacy-charm blocks), so channel alone isn't enough to determine first-met
        # coverage. Exercise every base referenced anywhere in this charm's overrides,
        # plus `None` for base-agnostic matching, against every published channel.
        ubuntu_versions: set[str | None] = {None}
        for entry in global_overrides.overrides:
            for criterion in entry.criteria:
                ubuntu_versions |= _referenced_ubuntu_versions(criterion)

        remaining = {(c, v) for c in client.get_charm_channels(charm_name) for v in ubuntu_versions}
        for override in global_overrides.overrides:
            matched = [(c, v) for c, v in remaining if override.meets(c, v)]
            remaining = {cv for cv in remaining if cv not in matched}
            if not matched:
                criteria_repr = [c.model_dump(exclude_none=True) for c in override.criteria]
                unmatched.append(f"{charm_name}: override with criteria={criteria_repr} matches no published channels")
                continue
            for channel, ubuntu_version in matched if all_channels else matched[:1]:
                params.append((charm_name, channel, ubuntu_version))
                ids.append(f"{charm_name}[{channel}][{ubuntu_version}]")

    if unmatched:
        pytest.fail("One or more overrides match no published channels:\n" + "\n".join(f"  - {m}" for m in unmatched))


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
