# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

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
from bundle_builder_x.release_errors import ReleaseUnavailableError


def test_charm_override_yaml_is_valid(
    charm_name: str,
    overrides_client: OverridesClient,
) -> None:
    assert overrides_client.overrides is not None
    raw = yaml.safe_load((overrides_client.overrides / f"{charm_name}.yaml").read_text())
    CharmGlobalOverrides.model_validate(raw)


def test_charm_override_file_is_valid(
    charm_channel: tuple[str, CharmChannel, str | None],
    overrides_charmhub_client: CharmhubClient,
) -> None:
    charm_name, channel, ubuntu_version = charm_channel
    try:
        overrides_charmhub_client.charm_from_store(
            charm_name=charm_name,
            ubuntu_arch="amd64",
            charm_track=channel.track,
            charm_risk=channel.risk,
            ubuntu_version=ubuntu_version,
        )
    except UnparsableCharmException as exc:
        pytest.fail(str(exc))
    except ReleaseUnavailableError:
        # This (channel, ubuntu_version) combination isn't actually published for this
        # charm (e.g. an old track never released a revision for a newer base, or vice
        # versa) - unrelated to whether the override's declared endpoints are stale.
        pytest.skip(f"no release published for {charm_name} at {channel} / ubuntu {ubuntu_version}")
