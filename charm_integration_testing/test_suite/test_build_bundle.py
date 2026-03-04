# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from typing import Callable

import pytest

from bundle_builder.bundle import Application, ApplicationEndpoint, Bundle, Integration  # type: ignore[import-untyped]
from bundle_builder.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder.charmhub import CharmhubClient  # type: ignore[import-untyped]
from bundle_builder.charmhub_http import CharmReleaseNotFoundException  # type: ignore[import-untyped]
from bundle_builder.overrides import OverridesClient  # type: ignore[import-untyped]

from .scheduler.states import State

_STATIC_DIR = Path(__file__).parent.parent.parent / "static"

_PLATFORM_BY_SUBSTRATE: dict[str, str] = {
    "kubernetes": "kubernetes",
    "openstack": "machine",
}


@pytest.mark.state(requires=State.NO_BUNDLE, provides=State.BUNDLE_BUILT)
def test_build_bundle(
    charm_under_test: str,
    neighbor_charm: str,
    charm_channel: str | None,
    charm_revision: int | None,
    charm_series: str | None,
    target_endpoint: str,
    neighbor_endpoint: str,
    substrate: str,
    bundle_output: Path,
    bundle_mermaid_output: Path,
    logger: logging.Logger,
) -> None:
    overrides_client = OverridesClient(
        charm_metadata_overrides=_STATIC_DIR / "charm-metadata-overrides",
        charm_platform_overrides=_STATIC_DIR / "charm-platform-overrides",
        charm_listing_overrides=_STATIC_DIR / "charm-listing-overrides.yaml",
        charm_test_configs=_STATIC_DIR / "charm-test-configs",
        charm_priorities_config=_STATIC_DIR / "charm-priorities.yaml",
        charm_default_versions=_STATIC_DIR / "charm-default-versions.yaml",
    )
    charmhub_client = CharmhubClient(logger=logger, overrides_client=overrides_client)

    try:
        target_charm = charmhub_client.charm_from_store(
            charm_name=charm_under_test,
            charm_channel=charm_channel,
            charm_revision=charm_revision,
            ubuntu_version=charm_series,
            ubuntu_arch="amd64",
        )
    except CharmReleaseNotFoundException as e:
        pytest.fail(f"Charm release not found for '{charm_under_test}': {e}")

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
                Application(name="target", charm=target_charm),
                Application(name="neighbor", charm=neighbor),
            }
        ),
        integrations=frozenset({integration}),
        platform=_PLATFORM_BY_SUBSTRATE[substrate],
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


@pytest.mark.state(requires=State.BUNDLE_BUILT, provides=State.EMPTY_MODEL)
def test_verify_bundle(bundle_output: Path) -> None:
    # Notes(mbenzan): Stub logic just to transition from BUNDLE_BUILT to EMPTY_MODEL.
    # In the future we could add any other verification we like before
    # the controller gets created and move the transition to NO_CONTROLLER.
    assert Path(bundle_output).exists()


@pytest.mark.state(requires=State.BUNDLE_BUILT)
def test_write_bundle_to_github(
    bundle_output: Path, bundle_mermaid_output: Path, log_to_github_step_summary: Callable[[str], None]
) -> None:
    mermaid_diagram = bundle_mermaid_output.read_text()
    juju_bundle = bundle_output.read_text()

    log = f""""### Generated Bundle Mermaid Diagram"
    ```mermaid
    {mermaid_diagram}
    ```

    ### Generated Juju Bundle
    ```yaml
    {juju_bundle}
    ```
    """
    log_to_github_step_summary(log)
