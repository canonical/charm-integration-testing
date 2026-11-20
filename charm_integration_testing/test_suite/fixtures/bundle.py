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
    CharmReleaseNotFoundException,
    IntegrationSpec,
    ModelSpec,
    Solution,
    SpecFile,
    UnsatisfiableBundleError,
)
from bundle_builder_x import (
    JujuVersion as BundleJujuVersion,
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


class _TargetCharmSelectors(BaseModel):
    charm: str
    channel: CharmChannel | None = None
    revision: int | None = Field(default=None, strict=True, ge=0)
    base: str | None = Field(default=None, pattern=r"^ubuntu@\d+\.\d+$")


class _BundleDocument(BaseModel):
    applications: dict[str, object]


@pytest.fixture(scope="session")
def target_bundle_charm_selectors(target_bundle: Path, target_application: str) -> _TargetCharmSelectors:
    """Read the target's Charmhub selectors without validating unrelated applications."""
    documents = yaml.safe_load_all(target_bundle.read_text(encoding="utf-8"))
    bundle = _BundleDocument.model_validate(next(documents, None))
    return _TargetCharmSelectors.model_validate(bundle.applications[target_application])


@pytest.fixture(scope="session")
def target_resolved_charm(
    request: pytest.FixtureRequest,
    target_application: str,
    target_controller: str,
    model: str,
) -> Charm | None:
    """Resolve the target, returning None when expected release constraints cannot be met."""
    try:
        if State(request.config.getoption("--current-state")) == State.NO_BUNDLE:
            solution: Solution = request.getfixturevalue("bundle_solution")
            model_key = f"{target_controller}/{model}"
            for bundle in solution.bundles:
                if bundle.model == model_key:
                    return bundle.applications[target_application].charm
            raise ValueError(f"Generated solution has no bundle for model {model_key!r}.")

        application: _TargetCharmSelectors = request.getfixturevalue("target_bundle_charm_selectors")
        channel = application.channel
        client: CharmhubClient = request.getfixturevalue("charmhub_client")
        arch: str = request.getfixturevalue("target_arch")
        platform: str = request.getfixturevalue("target_platform")
        juju_cli_version: JujuVersion = request.getfixturevalue("juju_cli_version")
        return client.charm_from_store(
            charm_name=application.charm,
            ubuntu_arch=arch,
            platform=platform,
            juju_version=BundleJujuVersion.parse(str(juju_cli_version)),
            charm_track=channel.track if channel is not None else None,
            charm_risk=channel.risk if channel is not None else None,
            charm_branch=(channel.branch or None) if channel is not None else None,
            charm_revision=application.revision,
            ubuntu_version=application.base.removeprefix("ubuntu@") if application.base is not None else None,
        )
    except (UnsatisfiableBundleError, CharmReleaseNotFoundException):
        return None


@pytest.fixture(scope="session")
def target_resolved_charm_for_refresh(request: pytest.FixtureRequest) -> Charm:
    """Require a recorded original revision before starting or resuming a refresh cycle."""
    if State(request.config.getoption("--current-state")) != State.NO_BUNDLE:
        application: _TargetCharmSelectors = request.getfixturevalue("target_bundle_charm_selectors")
        if application.revision is None:
            pytest.skip("The saved bundle has no target revision to restore after downgrade.")
    target: Charm | None = request.getfixturevalue("target_resolved_charm")
    if target is None:
        pytest.skip("The target charm could not be resolved for the refresh cycle.")
    return target
