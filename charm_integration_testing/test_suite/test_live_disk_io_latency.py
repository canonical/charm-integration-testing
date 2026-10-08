# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from time import sleep

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle

from .scheduler.states import State

# Global default latency target. Per-charm overrides are tracked separately.
VOLUME_PATH = "/"
DELAY = timedelta(milliseconds=500)
PERCENT = 100
LATENCY_DURATION = timedelta(minutes=10)
# Disk I/O latency recovers on the update-status hook interval, so allow a few cycles.
RECOVER_TIMEOUT = timedelta(minutes=15)


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_live_disk_io_latency(
    juju_client: JujuClient,
    require_chaos_tool: MetaChaosClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    neighbor_model_ref: JujuModelHandle | None,
) -> None:
    models = list(dict.fromkeys([target_model_ref, *([neighbor_model_ref] if neighbor_model_ref else [])]))
    juju_client.multi_model_idle_for_period(models=models, timeout=RECOVER_TIMEOUT, strict_timeout=True)
    unit = f"{target_application}/0"

    try:
        # Chaos-Mesh-only (no client implements io_latency otherwise): the fixture skips the
        # test automatically via on_unsupported when Chaos Mesh is absent. io_latency() blocks
        # until Chaos Mesh's controller confirms the IOChaos resource was actually injected.
        require_chaos_tool.io_latency(
            model=target_model_ref,
            unit=unit,
            volume_path=VOLUME_PATH,
            delay=DELAY,
            percent=PERCENT,
            duration=LATENCY_DURATION,
        )
        # A healthy workload may remain active throughout the fault.
        sleep(LATENCY_DURATION.total_seconds())
    finally:
        # Cleans up every pending experiment on this unit; io_latency is the only one active
        # here, and the exact dispatched path may differ from VOLUME_PATH when a per-charm
        # override redirects it, so a path-scoped cleanup() call cannot be relied on.
        require_chaos_tool.cleanup_all()

    # Recovery must happen without operator intervention.
    juju_client.multi_model_idle_for_period(models=models, timeout=RECOVER_TIMEOUT, strict_timeout=True)
    for model in models:
        juju_client.validate_model(model=model, level="deep")
