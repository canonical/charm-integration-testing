# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import pytest
import yaml
from juju import JujuVersion
from pydantic import BaseModel, Field

from bundle_builder_x import (
    AppSpec,
    BundleBuilder,
    Charm,
    CharmChannel,
    CharmhubClient,
    IntegrationSpec,
    ModelSpec,
    Solution,
    SpecFile,
)

from ..scheduler.states import State


@pytest.fixture(scope="session")
def bundle_solution(
    bundle_builder: BundleBuilder,
    juju_cli_version: JujuVersion,
    target_application: str,
    target_channel: str | None,
    target_charm: str,
    target_controller: str,
    target_endpoint: str,
    model: str,
    target_platform: str,
    target_arch: str,
    target_revision: int | None,
    target_series: str | None,
    neighbor_application: str,
    neighbor_charm: str,
    neighbor_controller: str | None,
    neighbor_endpoint: str,
    neighbor_model: str | None,
    neighbor_platform: str,
    neighbor_arch: str,
    is_cmr_test: bool,
) -> Solution:
    """Synthesize the immutable test plan once, including all integration constraints."""
    target_app_spec = AppSpec(
        charm=target_charm,
        channel=target_channel,
        revision=target_revision,
        base=target_series,
    )
    neighbor_app_spec = AppSpec(charm=neighbor_charm)
    if not is_cmr_test:
        spec = SpecFile(
            models=[
                ModelSpec(
                    name=model,
                    platform=target_platform,
                    arch=target_arch,
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
        spec = SpecFile(
            models=[
                ModelSpec(
                    name=model,
                    platform=target_platform,
                    arch=target_arch,
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
                    arch=neighbor_arch,
                    juju=str(juju_cli_version),
                    controller=neighbor_controller,
                    applications={neighbor_application: neighbor_app_spec},
                ),
            ]
        )
    return bundle_builder.build(spec)


class _PinnedApplication(BaseModel):
    charm: str
    channel: CharmChannel
    revision: int = Field(strict=True, ge=0)
    base: str = Field(pattern=r"^ubuntu@\d+\.\d+$")


class _SavedBundle(BaseModel):
    applications: dict[str, _PinnedApplication]


@pytest.fixture(scope="session")
def target_resolved_charm(
    request: pytest.FixtureRequest,
    target_application: str,
    target_controller: str,
    model: str,
) -> Charm:
    """Return the target selected for this run, not a separately resolved default."""
    if State(request.config.getoption("--current-state")) == State.NO_BUNDLE:
        solution: Solution = request.getfixturevalue("bundle_solution")
        model_key = f"{target_controller}/{model}"
        for bundle in solution.bundles:
            if bundle.model == model_key:
                return bundle.applications[target_application].charm
        raise ValueError(f"Generated solution has no bundle for model {model_key!r}.")

    # Resumed runs must retain the saved deployment plan, even if defaults have moved.
    bundle_path: Path = request.getfixturevalue("target_bundle")
    documents = yaml.safe_load_all(bundle_path.read_text(encoding="utf-8"))
    saved = _SavedBundle.model_validate(next(documents, None))
    application = saved.applications[target_application]
    client: CharmhubClient = request.getfixturevalue("charmhub_client")
    arch: str = request.getfixturevalue("target_arch")
    return client.charm_from_store(
        charm_name=application.charm,
        ubuntu_arch=arch,
        charm_track=application.channel.track,
        charm_risk=application.channel.risk,
        charm_branch=application.channel.branch or None,
        charm_revision=application.revision,
        ubuntu_version=application.base.removeprefix("ubuntu@"),
    )
