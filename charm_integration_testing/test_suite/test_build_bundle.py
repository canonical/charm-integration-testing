# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import pytest
from juju import JujuVersion

from bundle_builder_x import (
    AppSpec,
    BundleBuilder,
    CharmhubClient,
    IntegrationSpec,
    ModelSpec,
    OverridesClient,
    SpecFile,
)

from .scheduler.states import State


@pytest.mark.state(requires=State.NO_BUNDLE, provides=State.NO_CONTROLLER)
def test_build_bundle(
    bundle_mermaid_output: Path,
    charm_overrides: Path,
    logger: logging.Logger,
    juju_cli_version: JujuVersion,
    target_application: str,
    target_bundle: Path,
    target_channel: str | None,
    target_charm: str,
    target_controller: str,
    target_endpoint: str,
    model: str,
    target_platform: str,
    target_revision: int | None,
    target_series: str | None,
    neighbor_application: str,
    neighbor_bundle: Path | None,
    neighbor_charm: str,
    neighbor_controller: str | None,
    neighbor_endpoint: str,
    neighbor_model: str | None,
    neighbor_platform: str,
) -> None:
    overrides_client = OverridesClient(overrides=charm_overrides, logger=logger)
    charmhub_client = CharmhubClient(logger=logger, overrides_client=overrides_client)

    target_app_spec = AppSpec(
        charm=target_charm,
        channel=target_channel,
        revision=target_revision,
        base=target_series,
    )
    neighbor_app_spec = AppSpec(charm=neighbor_charm)

    if neighbor_bundle is None:
        # Single-model (non-CMR) build
        spec = SpecFile(
            models=[
                ModelSpec(
                    name=model,
                    platform=target_platform,
                    arch="amd64",
                    # TODO: Juju model isn't bootstrapped until later in the test setup,
                    # so we can't resolve the Juju version here. We should refactor to
                    # support the user passing in the juju version for target and neighbor.
                    juju=str(juju_cli_version),
                    controller=target_controller,
                    applications={
                        target_application: target_app_spec,
                        neighbor_application: neighbor_app_spec,
                    },
                    integrations=[
                        IntegrationSpec(
                            application=target_application,
                            endpoint=target_endpoint,
                            remote_application=neighbor_application,
                            remote_endpoint=neighbor_endpoint,
                        ),
                    ],
                ),
            ]
        )
    else:
        assert neighbor_controller is not None
        assert neighbor_model is not None
        # Cross-model relation (CMR) build
        spec = SpecFile(
            models=[
                ModelSpec(
                    name=model,
                    platform=target_platform,
                    arch="amd64",
                    juju=str(juju_cli_version),
                    controller=target_controller,
                    applications={target_application: target_app_spec},
                    integrations=[
                        IntegrationSpec(
                            application=target_application,
                            endpoint=target_endpoint,
                            remote_application=neighbor_application,
                            remote_endpoint=neighbor_endpoint,
                            remote_model=neighbor_model,
                            remote_controller=neighbor_controller,
                        ),
                    ],
                ),
                ModelSpec(
                    name=neighbor_model,
                    platform=neighbor_platform,
                    arch="amd64",
                    juju=str(juju_cli_version),
                    controller=neighbor_controller,
                    applications={neighbor_application: neighbor_app_spec},
                ),
            ]
        )

    bundle_builder = BundleBuilder(charmhub_client=charmhub_client, logger=logger)
    solution = bundle_builder.build(spec)

    separator = "-" * 80
    bundles_by_model = {b.model: b for b in solution.bundles}

    target_model_key = f"{target_controller}/{model}"
    target_contents = bundles_by_model[target_model_key].export()
    target_bundle.write_text(target_contents, encoding="utf-8")
    logger.info(f"Bundle written to {target_bundle}.")
    logger.info(f"Bundle content:\n{separator}\n{target_contents.strip()}\n{separator}")

    if neighbor_bundle is not None:
        assert neighbor_model is not None
        assert neighbor_controller is not None
        neighbor_model_key = f"{neighbor_controller}/{neighbor_model}"
        neighbor_contents = bundles_by_model[neighbor_model_key].export()
        neighbor_bundle.write_text(neighbor_contents, encoding="utf-8")
        logger.info(f"Bundle written to {neighbor_bundle}.")
        logger.info(f"Bundle content:\n{separator}\n{neighbor_contents.strip()}\n{separator}")

    bundle_mermaid_output.write_text(solution.export_mermaid(), encoding="utf-8")
    logger.info(f"Bundle Mermaid diagram written to {bundle_mermaid_output}")
