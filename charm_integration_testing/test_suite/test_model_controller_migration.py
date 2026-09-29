# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuBackend, JujuClient, JujuModelHandle

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_model_controller_migration(
    juju_client: JujuClient,
    juju_backend: JujuBackend,
    target_controller: str,
    temp_juju_controller: str,
    model: str,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
) -> None:
    temp_model_ref = JujuModelHandle(controller=temp_juju_controller, model=model)

    # Model migration is broken in juju >= 4.0.0: the client requires a MigrationTarget v8
    # facade the 4.0.14 server doesn't implement, blocking all password-authenticated migration.
    # See https://github.com/juju/juju/issues/23281
    if juju_client.version(target_model_ref).major >= 4:
        pytest.skip("Model migration is not supported on juju >= 4.0.0 (https://github.com/juju/juju/issues/23281).")

    # Validate all applications and relations before migration. Only target_model_ref's controller
    # is migrated below; the neighbor model stays on its own controller throughout, but is included
    # here and after each migration step for a CMR (the persistence validator lives on the
    # neighbor's requirer units).
    for model_ref in (m for m in (target_model_ref, neighbor_model_ref) if m is not None):
        juju_client.validate_model(model=model_ref, level="deep")

    juju_client.migrate_model(
        model_name=model, source_controller=target_controller, target_controller=temp_juju_controller
    )

    # Wait migration to start
    juju_client.wait_for_model_to_exist(model=temp_model_ref, timeout=timedelta(minutes=15))

    models_to_validate = [m for m in (temp_model_ref, neighbor_model_ref) if m is not None]

    # Wait until model is idle in new controller
    juju_client.multi_model_idle_for_period(models_to_validate, timeout=timedelta(minutes=15))

    # Workaround for https://github.com/juju/juju/issues/22114: CAAS workers don't
    # restart after migration, hanging `juju exec`. K8s-only.
    if juju_backend.is_k8s_controller(temp_juju_controller):
        juju_client.reboot_model_controller(model=temp_model_ref)
        juju_client.multi_model_idle_for_period(models_to_validate, timeout=timedelta(minutes=15))

    # The model kept its units and relation ids, but its controller name changed. The extension
    # re-keyed its tracked state on post_migrate_model, so the checkpoint finds the state
    # seeded before migration.

    # Validate all applications and relations AFTER migration
    for model_ref in models_to_validate:
        juju_client.validate_model(model=model_ref, level="deep")

    # Migrate the model back to the original controller
    juju_client.migrate_model(
        model_name=model, source_controller=temp_juju_controller, target_controller=target_controller
    )

    # Wait migration to start
    juju_client.wait_for_model_to_exist(model=target_model_ref, timeout=timedelta(minutes=15))

    models_to_validate_back = [m for m in (target_model_ref, neighbor_model_ref) if m is not None]

    # Wait until model is idle in old controller
    juju_client.multi_model_idle_for_period(models_to_validate_back, timeout=timedelta(minutes=15))

    # Workaround for https://github.com/juju/juju/issues/22114: CAAS workers on the
    # original controller don't restart after the return migration. K8s-only.
    if juju_backend.is_k8s_controller(target_controller):
        juju_client.reboot_model_controller(model=target_model_ref)
        juju_client.multi_model_idle_for_period(models_to_validate_back, timeout=timedelta(minutes=15))

    # Validate all applications and relations AFTER second migration
    for model_ref in models_to_validate_back:
        juju_client.validate_model(model=model_ref, level="deep")
