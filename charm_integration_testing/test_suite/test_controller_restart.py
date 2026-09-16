# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle, PersistenceKey

from validators.base import PersistenceState

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_controller_restart(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    persistence_state: dict[PersistenceKey, PersistenceState],
) -> None:
    # Reboot our controllers with a rolling reboot
    juju_client.reboot_model_controller(model=target_model_ref)

    # Wait until idle. Also wait on the neighbor model (unaffected by the reboot itself, but the
    # CMR relation may process events there as the target model recovers), so the checkpoint below
    # doesn't race a neighbor-side databag/hook that hasn't settled yet.
    models_to_settle = [target_model_ref] + ([neighbor_model_ref] if neighbor_model_ref is not None else [])
    juju_client.multi_model_idle_for_period(models_to_settle, timeout=timedelta(minutes=15))

    # Validate all applications and relations, and verify canary data survived the reboot. For a
    # CMR where target_model_ref's application is the provider, the applicable persistence
    # validator and tracked canary state live on the neighbor's requirer units instead, so
    # checkpoint the neighbor model too when present.
    for model_ref in (m for m in (target_model_ref, neighbor_model_ref) if m is not None):
        juju_client.validate_model(
            model=model_ref, level="deep", persistence="checkpoint", persistence_state=persistence_state
        )
