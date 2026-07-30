# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_model_controller_migration(
    juju_client: JujuClient,
    target_controller: str,
    temp_juju_controller: str,
    model: str,
) -> None:
    # Model migration is broken in juju >= 4.0.0.
    # See https://github.com/juju/juju/issues/22239
    if juju_client.version(f"{target_controller}:{model}").major >= 4:
        pytest.skip("Model migration is not supported on juju >= 4.0.0 (https://github.com/juju/juju/issues/22239).")

    # Validate all applications and relations before migration
    juju_client.validate_model(model=f"{target_controller}:{model}", level="deep")

    juju_client.migrate_model(
        model_name=model, source_controller=target_controller, target_controller=temp_juju_controller
    )

    # Wait migration to start
    juju_client.wait_for_model_to_exist(model=f"{temp_juju_controller}:{model}", timeout=timedelta(minutes=15))

    # Wait until model is idle in new controller
    juju_client.idle_for_period(model=f"{temp_juju_controller}:{model}", timeout=timedelta(minutes=15))

    # Workaround for https://github.com/juju/juju/issues/22114: CAAS workers don't
    # restart after migration, so `juju exec` to the operator pod hangs. Same bug as
    # the return leg below, just also needed here on the forward leg.
    juju_client.reboot_model_controller(model=f"{temp_juju_controller}:{model}")
    juju_client.idle_for_period(model=f"{temp_juju_controller}:{model}", timeout=timedelta(minutes=15))

    # Validate all applications and relations AFTER migration
    juju_client.validate_model(model=f"{temp_juju_controller}:{model}", level="deep")

    # Migrate the model back to the original controller
    juju_client.migrate_model(
        model_name=model, source_controller=temp_juju_controller, target_controller=target_controller
    )

    # Wait migration to start
    juju_client.wait_for_model_to_exist(model=f"{target_controller}:{model}", timeout=timedelta(minutes=15))

    # Wait until model is idle in old controller
    juju_client.idle_for_period(model=f"{target_controller}:{model}", timeout=timedelta(minutes=15))

    # Workaround for https://github.com/juju/juju/issues/22114:
    # After migrating back to the original controller, the per-app CAAS workers on the
    # original controller fail to restart, so the {app}-application-config K8s secrets
    # are never updated with the original controller's addresses. Restarting the
    # controller forces the CAAS workers to restart, which triggers Ensure() and
    # rewrites the secrets before the temp controller is destroyed.
    juju_client.reboot_model_controller(model=f"{target_controller}:{model}")
    juju_client.idle_for_period(model=f"{target_controller}:{model}", timeout=timedelta(minutes=15))

    # Validate all applications and relations AFTER second migration
    juju_client.validate_model(model=f"{target_controller}:{model}", level="deep")
