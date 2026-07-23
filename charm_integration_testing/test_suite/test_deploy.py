# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

import pytest
from juju import JujuClient
from juju.bundle_utils import parse_charm_names_from_bundle

from .scheduler.states import State

# Default wait window for a bundle to reach idle.
_DEFAULT_IDLE_TIMEOUT = timedelta(minutes=15)

# Extended wait window applied when a bundle contains any charm known to need materially
# longer to settle its relations (see https://github.com/canonical/charm-integration-testing/issues/794):
# postgresql-k8s/postgresql can take longer than _DEFAULT_IDLE_TIMEOUT to publish the
# relation data that charms like pgbouncer-k8s block on, especially under CI resource
# contention, without either side being stuck.
_EXTENDED_IDLE_TIMEOUT = timedelta(minutes=25)
_SLOW_SETTLING_CHARMS = {"postgresql-k8s", "postgresql"}


def _idle_timeout_for_bundle(bundle: Path) -> timedelta:
    charm_names = parse_charm_names_from_bundle(bundle.read_text(encoding="utf-8"))
    if charm_names & _SLOW_SETTLING_CHARMS:
        return _EXTENDED_IDLE_TIMEOUT
    return _DEFAULT_IDLE_TIMEOUT


@pytest.mark.state(requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
def test_deploy(
    juju_client: JujuClient,
    target_bundle: Path,
    neighbor_bundle: Path | None,
    model: str,
    target_controller: str,
    neighbor_model: str | None,
    neighbor_controller: str | None,
    tmp_path: Path,
) -> None:
    target_model_uri = f"{target_controller}:{model}"
    all_bundles: list[tuple[Path, str]] = [(target_bundle, target_model_uri)]
    if neighbor_bundle is not None:
        assert neighbor_controller is not None
        assert neighbor_model is not None
        all_bundles.append((neighbor_bundle, f"{neighbor_controller}:{neighbor_model}"))

    juju_client.deploy_bundles(all_bundles, tmp_path)

    # TODO: Add multi-model wait
    # https://github.com/canonical/charm-integration-testing/issues/515
    for bundle, model_uri in all_bundles:
        juju_client.idle_for_period(model=model_uri, timeout=_idle_timeout_for_bundle(bundle))

    for _, model_uri in all_bundles:
        juju_client.validate_model(model=model_uri, level="deep")
