# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest

from bundle_builder_x.charm import CharmChannel
from bundle_builder_x.charmhub_http import IncompleteCharmInfoException, InfoResponse
from bundle_builder_x.overrides import CharmGlobalOverrides

from ..overrides.conftest import _override_channel_bases


def _channel_map(*releases: tuple[str, str, str]) -> InfoResponse:
    return InfoResponse.model_validate(
        {
            "channel-map": [
                {
                    "channel": {
                        "name": channel,
                        "track": "latest",
                        "risk": channel,
                        "base": {"name": "ubuntu", "channel": base, "architecture": arch},
                    }
                }
                for channel, base, arch in releases
            ]
        }
    )


def test_all_channels_checks_every_published_base() -> None:
    # GIVEN a channel serving several bases, including an architecture-independent release
    info = _channel_map(
        ("beta", "20.04", "amd64"),
        ("beta", "22.04", "amd64"),
        ("beta", "24.04", "amd64"),
        ("beta", "24.04", "amd64"),
        ("beta", "18.04", "all"),
        ("edge", "22.04", "arm64"),
    )
    overrides = CharmGlobalOverrides.model_validate({"overrides": [{}]})

    # WHEN selecting all releases for a base-agnostic override
    selected = _override_channel_bases(info, overrides, all_channels=True)

    # THEN real amd64 bases are tested once each, without a synthetic base
    beta = CharmChannel.model_validate("beta")
    assert selected == [(beta, "18.04"), (beta, "20.04"), (beta, "22.04"), (beta, "24.04")]


@pytest.mark.parametrize("all_channels", [False, True])
def test_base_specific_overrides_take_priority(all_channels: bool) -> None:
    # GIVEN a specific base block before a catch-all block
    info = _channel_map(
        ("stable", "20.04", "amd64"),
        ("stable", "22.04", "amd64"),
        ("stable", "24.04", "amd64"),
    )
    overrides = CharmGlobalOverrides.model_validate({"overrides": [{"criteria": [{"ubuntu_version": "20.04"}]}, {}]})

    # WHEN selecting releases
    selected = _override_channel_bases(info, overrides, all_channels)

    # THEN each release belongs only to its first matching block
    stable = CharmChannel.model_validate("stable")
    expected = [(stable, "20.04"), (stable, "22.04")]
    if all_channels:
        expected.append((stable, "24.04"))
    assert selected == expected


def test_unpublished_channel_base_combination_does_not_satisfy_criteria() -> None:
    # GIVEN a base published only on edge, not stable
    info = _channel_map(("stable", "22.04", "amd64"), ("edge", "24.04", "amd64"))
    overrides = CharmGlobalOverrides.model_validate(
        {"overrides": [{"criteria": [{"risk": "stable", "ubuntu_version": "24.04"}]}]}
    )

    # WHEN selecting releases, THEN an impossible criteria block is reported
    with pytest.raises(ValueError, match="matches no published"):
        _override_channel_bases(info, overrides, all_channels=True)


def test_missing_channel_base_is_reported() -> None:
    # GIVEN incomplete channel-map metadata
    info = InfoResponse.model_validate(
        {"channel-map": [{"channel": {"name": "stable", "track": "latest", "risk": "stable"}}]}
    )
    overrides = CharmGlobalOverrides.model_validate({"overrides": [{}]})

    # WHEN selecting releases, THEN missing metadata cannot produce a false green
    with pytest.raises(IncompleteCharmInfoException, match="Missing base"):
        _override_channel_bases(info, overrides, all_channels=True)


def test_non_ubuntu_bases_are_excluded() -> None:
    # GIVEN Ubuntu and non-Ubuntu releases on the same channel
    info = InfoResponse.model_validate(
        {
            "channel-map": [
                {
                    "channel": {
                        "name": "stable",
                        "track": "latest",
                        "risk": "stable",
                        "base": {"name": name, "channel": base, "architecture": "amd64"},
                    }
                }
                for name, base in [("ubuntu", "22.04"), ("centos", "7")]
            ]
        }
    )
    overrides = CharmGlobalOverrides.model_validate({"overrides": [{}]})

    # WHEN selecting releases, THEN only supported Ubuntu bases are validated
    assert _override_channel_bases(info, overrides, all_channels=True) == [
        (CharmChannel.model_validate("stable"), "22.04")
    ]


def test_shadowed_override_is_reported() -> None:
    # GIVEN two catch-all blocks, so the second block is unreachable
    info = _channel_map(("stable", "22.04", "amd64"))
    overrides = CharmGlobalOverrides.model_validate({"overrides": [{}, {}]})

    # WHEN selecting releases, THEN first-match semantics expose the shadowed block
    with pytest.raises(ValueError, match="matches no published"):
        _override_channel_bases(info, overrides, all_channels=True)
