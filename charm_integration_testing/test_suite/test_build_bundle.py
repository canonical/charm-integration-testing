# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import pytest

from bundle_builder import (
    Application,
    ApplicationEndpoint,
    Bundle,
    BundleBuilder,
    CharmhubClient,
    Integration,
    JujuVersion,
    OverridesClient,
)

from .scheduler.states import State


@pytest.mark.state(requires=State.NO_BUNDLE, provides=State.NO_CONTROLLER)
def test_build_bundle(
    target_charm: str,
    neighbor_charm: str,
    target_channel: str | None,
    target_revision: int | None,
    target_series: str | None,
    target_endpoint: str,
    neighbor_endpoint: str,
    platform: str,
    juju_cli_version: str,
    charm_metadata_overrides: Path,
    charm_platform_overrides: Path,
    charm_listing_overrides: Path,
    charm_test_configs: Path,
    charm_priorities_config: Path,
    charm_default_versions: Path,
    target_application: str,
    neighbor_application: str,
    bundle: Path,
    bundle_mermaid_output: Path,
    logger: logging.Logger,
) -> None:
    overrides_client = OverridesClient(
        charm_metadata_overrides=charm_metadata_overrides,
        charm_platform_overrides=charm_platform_overrides,
        charm_listing_overrides=charm_listing_overrides,
        charm_test_configs=charm_test_configs,
        charm_priorities_config=charm_priorities_config,
        charm_default_versions=charm_default_versions,
    )
    charmhub_client = CharmhubClient(logger=logger, overrides_client=overrides_client)

    fetched_target_charm = charmhub_client.charm_from_store(
        charm_name=target_charm,
        charm_channel=target_channel,
        charm_revision=target_revision,
        ubuntu_version=target_series,
        ubuntu_arch="amd64",
    )

    neighbor = charmhub_client.charm_from_store(
        charm_name=neighbor_charm,
        ubuntu_arch="amd64",
    )

    integration: Integration = frozenset(
        {
            ApplicationEndpoint(target_application, target_endpoint),
            ApplicationEndpoint(neighbor_application, neighbor_endpoint),
        }
    )
    base_bundle = Bundle(
        applications=frozenset(
            {
                Application(name=target_application, charm=fetched_target_charm),
                Application(name=neighbor_application, charm=neighbor),
            }
        ),
        integrations=frozenset({integration}),
        platform=platform,
        arch="amd64",
        juju_version=JujuVersion.parse(juju_cli_version),
    )

    bundle_builder = BundleBuilder(charmhub_client=charmhub_client, logger=logger)
    built_bundle = bundle_builder.build(base_bundle)

    bundle_contents = built_bundle.export()
    separator = "-" * 80
    bundle.write_text(bundle_contents, encoding="utf-8")
    logger.info(f"Bundle written to {bundle}.")
    logger.info(f"Bundle content:\n{separator}\n{bundle_contents.strip()}\n{separator}")

    bundle_mermaid_output.write_text(built_bundle.export_mermaid(), encoding="utf-8")
    logger.info(f"Bundle Mermaid diagram written to {bundle_mermaid_output}")
