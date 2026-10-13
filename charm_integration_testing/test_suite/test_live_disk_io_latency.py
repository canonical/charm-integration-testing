# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from time import monotonic, sleep

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle

from .scheduler.states import State

# Global default latency target. Per-charm overrides are tracked separately.
VOLUME_PATH = "/"
DELAY = timedelta(milliseconds=500)
PERCENT = 100
LATENCY_DURATION = timedelta(minutes=10)
# Delay between IOChaos activity checks during observation.
CHECK_INTERVAL = timedelta(seconds=10)
# Disk I/O latency recovers on the update-status hook interval, so allow a few cycles.
RECOVER_TIMEOUT = timedelta(minutes=15)


def observe_io_latency(
    chaos: MetaChaosClient, model: JujuModelHandle, unit: str, volume_path: str, seconds: float
) -> None:
    """Poll IOChaos health until the resolved duration elapses, detecting early interruption."""
    deadline = monotonic() + seconds
    while True:
        remaining = deadline - monotonic()
        exhausted = remaining <= 0
        chaos.check_io_latency(model, unit, volume_path, allow_completed=exhausted)
        if exhausted:
            return
        sleep(min(CHECK_INTERVAL.total_seconds(), remaining))


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_live_disk_io_latency(
    juju_client: JujuClient,
    require_chaos_tool: MetaChaosClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    neighbor_model_ref: JujuModelHandle | None,
) -> None:
    if not require_chaos_tool.supports("io_latency"):
        # Skip before the baseline wait, not after.
        pytest.skip(f"No available chaos client supports 'io_latency' for {target_model_ref.uri}.")

    models = list(dict.fromkeys([target_model_ref, *([neighbor_model_ref] if neighbor_model_ref else [])]))
    juju_client.multi_model_idle_for_period(models=models, timeout=RECOVER_TIMEOUT, strict_timeout=True)
    unit = f"{target_application}/0"

    observation_error: Exception | None = None
    try:
        # The experiment's duration starts counting down at creation, before io_latency()
        # returns, so track elapsed time across the call rather than starting a fresh window.
        injection_start = monotonic()
        # io_latency() blocks until Chaos Mesh's controller confirms the IOChaos resource was
        # actually injected.
        resolved_duration = require_chaos_tool.io_latency(
            model=target_model_ref,
            unit=unit,
            volume_path=VOLUME_PATH,
            delay=DELAY,
            percent=PERCENT,
            duration=LATENCY_DURATION,
        )
        # A per-charm override may have changed the experiment's actual duration, so observe for
        # as long as the fault will actually run rather than the (possibly stale) caller value,
        # minus the time already spent waiting for confirmed injection, and detect if the fault
        # stops or is removed before then.
        remaining = resolved_duration.total_seconds() - (monotonic() - injection_start)
        observe_io_latency(require_chaos_tool, target_model_ref, unit, VOLUME_PATH, max(0.0, remaining))
    except Exception as error:
        observation_error = error
        raise
    finally:
        try:
            require_chaos_tool.cleanup_all()
        except Exception as cleanup_error:
            if observation_error is not None:
                raise RuntimeError(
                    f"Disk I/O latency injection failed: {observation_error!r}; cleanup also failed: {cleanup_error!r}"
                ) from cleanup_error
            raise

    # Recovery must happen without operator intervention.
    juju_client.multi_model_idle_for_period(models=models, timeout=RECOVER_TIMEOUT, strict_timeout=True)
    for model in models:
        juju_client.validate_model(model=model, level="deep")
