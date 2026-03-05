# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import pytest

from bundle_builder.bundle import Application, ApplicationEndpoint, Bundle, Integration  # type: ignore[import-untyped]
from bundle_builder.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder.charmhub import CharmhubClient  # type: ignore[import-untyped]
from bundle_builder.charmhub_http import CharmReleaseNotFoundException  # type: ignore[import-untyped]
from bundle_builder.overrides import OverridesClient  # type: ignore[import-untyped]

from .scheduler.states import State


@pytest.mark.state(requires=State.NO_BUNDLE, provides=State.EMPTY_MODEL)
def test_build_bundle(
    target_charm: str,
    neighbor_charm: str,
    target_channel: str | None,
    target_revision: int | None,
    target_series: str | None,
    target_endpoint: str,
    neighbor_endpoint: str,
    platform: str,
    static_dir: Path,
    bundle_output: Path,
    bundle_mermaid_output: Path,
    logger: logging.Logger,
) -> None:
    overrides_client = OverridesClient(
        charm_metadata_overrides=static_dir / "charm-metadata-overrides",
        charm_platform_overrides=static_dir / "charm-platform-overrides",
        charm_listing_overrides=static_dir / "charm-listing-overrides.yaml",
        charm_test_configs=static_dir / "charm-test-configs",
        charm_priorities_config=static_dir / "charm-priorities.yaml",
        charm_default_versions=static_dir / "charm-default-versions.yaml",
    )
    charmhub_client = CharmhubClient(logger=logger, overrides_client=overrides_client)

    try:
        fetched_target_charm = charmhub_client.charm_from_store(
            charm_name=target_charm,
            charm_channel=target_channel,
            charm_revision=target_revision,
            ubuntu_version=target_series,
            ubuntu_arch="amd64",
        )
    except CharmReleaseNotFoundException as e:
        pytest.fail(f"Charm release not found for '{target_charm}': {e}")

    try:
        neighbor = charmhub_client.charm_from_store(
            charm_name=neighbor_charm,
            ubuntu_arch="amd64",
        )
    except CharmReleaseNotFoundException as e:
        pytest.fail(f"Charm release not found for '{neighbor_charm}': {e}")

    integration: Integration = frozenset(
        {
            ApplicationEndpoint("target", target_endpoint),
            ApplicationEndpoint("neighbor", neighbor_endpoint),
        }
    )
    base_bundle = Bundle(
        applications=frozenset(
            {
                Application(name="target", charm=fetched_target_charm),
                Application(name="neighbor", charm=neighbor),
            }
        ),
        integrations=frozenset({integration}),
        platform=platform,
        arch="amd64",
    )

    bundle_builder = BundleBuilder(charmhub_client=charmhub_client, logger=logger)
    try:
        built_bundle = bundle_builder.build(base_bundle)
    except UncompletableBundleError as e:
        pytest.fail(f"Could not complete bundle: {e}")

    bundle_output.write_text(built_bundle.export(), encoding="utf-8")
    logger.info(f"Bundle written to {bundle_output}")

    bundle_mermaid_output.write_text(built_bundle.export_mermaid(), encoding="utf-8")
    logger.info(f"Bundle Mermaid diagram written to {bundle_mermaid_output}")

    assert Path(bundle_output).exists()
