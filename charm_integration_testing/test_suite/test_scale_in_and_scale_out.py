# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle, PersistenceKey

from validators.base import PersistenceState

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED)
def test_scale_in_and_scale_out_charm(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    target_application: str,
    persistence_state: dict[PersistenceKey, PersistenceState],
) -> None:
    # Get units
    num_units = juju_client.num_units(target_application, model=target_model_ref)

    # Remove units
    juju_client.scale_application(target_application, 0, model=target_model_ref)

    # Wait for all units to be removed
    juju_client.wait_for_removal_of_units(target_application, model=target_model_ref, timeout=timedelta(minutes=10))

    # Rescale application
    juju_client.scale_application(target_application, num_units, model=target_model_ref)

    # Wait for return to idle. Also wait on the neighbor model (scaling changes the relation's
    # remote unit membership, which can trigger relation hooks there), so the neighbor-side
    # checkpoint below doesn't race a hook that hasn't settled yet.
    models_to_settle = [target_model_ref] + ([neighbor_model_ref] if neighbor_model_ref is not None else [])
    juju_client.multi_model_idle_for_period(models_to_settle, timeout=timedelta(minutes=15))

    # Validate all applications and relations. For a CMR where the target application is the
    # provider, the applicable persistence validator and tracked canary state live on the
    # neighbor's requirer units instead, so checkpoint the neighbor model too when present.
    for model_ref in (m for m in (target_model_ref, neighbor_model_ref) if m is not None):
        juju_client.validate_model(
            model=model_ref, level="simple", persistence="checkpoint", persistence_state=persistence_state
        )
