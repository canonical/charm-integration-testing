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
from bundle_builder_x.domain import Domain, DomainApplication, DomainModel, ModelRef, add_charm_to_domain
from bundle_builder_x.dsl_lowering import DSLLoweringError, LoweringContext, lower
from bundle_builder_x.juju_version import JujuVersion
from bundle_builder_x.overrides import CharmGlobalOverrides, OverridesClient
from bundle_builder_x.release_errors import ReleaseUnavailableError, ReleaseUnavailableKind


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
    except ReleaseUnavailableError as exc:
        if exc.kind is ReleaseUnavailableKind.UNEXPECTED_STORE_RESPONSE:
            # A malformed/unexpected Charmhub response is a real failure, not evidence
            # that this (channel, ubuntu_version) combination simply isn't published.
            raise
        # This (channel, ubuntu_version) combination isn't actually published for this
        # charm (e.g. an old track never released a revision for a newer base, or vice
        # versa) - unrelated to whether the override's declared endpoints are stale.
        pytest.skip(f"no release published for {charm_name} at {channel} / ubuntu {ubuntu_version}")


def test_charm_override_constraints_lower_against_charm_metadata(
    charm_channel: tuple[str, CharmChannel, str | None],
    overrides_charmhub_client: CharmhubClient,
) -> None:
    """charm_from_store only parses constraint syntax; a typo'd endpoint/config/feature name
    is only caught by lowering the constraint against this charm's real metadata."""
    charm_name, channel, ubuntu_version = charm_channel
    try:
        charm = overrides_charmhub_client.charm_from_store(
            charm_name=charm_name,
            ubuntu_arch="amd64",
            charm_track=channel.track,
            charm_risk=channel.risk,
            ubuntu_version=ubuntu_version,
        )
    except UnparsableCharmException:
        return  # already reported by test_charm_override_file_is_valid
    except ReleaseUnavailableError as exc:
        if exc.kind is ReleaseUnavailableKind.UNEXPECTED_STORE_RESPONSE:
            raise
        pytest.skip(f"no release published for {charm_name} at {channel} / ubuntu {ubuntu_version}")
        return

    if not charm.constraints:
        pytest.skip(f"{charm_name} has no override constraints to lower")

    model_ref = ModelRef(name="default")
    domain = Domain()
    domain.models[model_ref] = DomainModel(
        arch="amd64",
        platform=charm.platforms[0],
        juju_version=JujuVersion(major=3, minor=6, patch=0),
        applications={charm_name: DomainApplication(charm=charm_name)},
    )
    charm_id = add_charm_to_domain(charm, domain, model_ref)

    for expr in charm.constraints:
        ctx = LoweringContext(charm_id=charm_id, domain_charm=domain.charms[charm_id], domain=domain)
        try:
            lower(expr, ctx)
        except DSLLoweringError as exc:
            pytest.fail(f"{charm_name} ({channel}, ubuntu {ubuntu_version}): {exc}")
