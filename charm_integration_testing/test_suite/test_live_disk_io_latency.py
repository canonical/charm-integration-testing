# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Live Chaos Mesh IOChaos disk-latency lifecycle test.

This test does not perform or time actual disk I/O inside the workload; it verifies the
IOChaos experiment lifecycle instead, relying on Chaos Mesh's own confirmed-injection and
recovery signals (see chaos_mesh_client._experiment_injected): the fault is injected, remains
injected for its resolved duration without being stopped or interrupted early, and the unit
and its neighbor recover and pass deep validation afterwards.
"""

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
        exhausted = monotonic() >= deadline
        try:
            chaos.check_io_latency(model, unit, volume_path, allow_completed=exhausted)
        except RuntimeError:
            # The status read itself can be slow enough that the deadline passes while it is
            # in flight: the request was sent tolerating only active injection, but by the time
            # it failed the experiment may have completed normally. Recheck tolerating
            # completion before treating this as a genuine early-interruption failure.
            if exhausted or monotonic() < deadline:
                raise
            chaos.check_io_latency(model, unit, volume_path, allow_completed=True)
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        sleep(min(CHECK_INTERVAL.total_seconds(), remaining))


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
@pytest.mark.usefixtures("kubernetes_client")
def test_live_disk_io_latency(
    juju_client: JujuClient,
    require_chaos_tool: MetaChaosClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    neighbor_model_ref: JujuModelHandle | None,
) -> None:
    """Verify the IOChaos lifecycle: confirmed injection, no early stop, and recovery.

    Does not measure actual I/O latency in the workload; see module docstring.
    """
    if not require_chaos_tool.supports("io_latency"):
        # Skip before the baseline wait, not after.
        pytest.skip(f"No available chaos client supports 'io_latency' for {target_model_ref.uri}.")

    models = list(dict.fromkeys([target_model_ref, *([neighbor_model_ref] if neighbor_model_ref else [])]))
    juju_client.multi_model_idle_for_period(models=models, timeout=RECOVER_TIMEOUT, strict_timeout=True)
    unit = f"{target_application}/0"

    observation_error: BaseException | None = None
    try:
        # io_latency() blocks until Chaos Mesh's controller confirms the IOChaos resource was
        # actually injected. The returned injected_at is when the experiment resource was
        # actually created (its duration starts counting down then, not when this call
        # returns), so remaining observation time must be measured from that point.
        injection = require_chaos_tool.io_latency(
            model=target_model_ref,
            unit=unit,
            volume_path=VOLUME_PATH,
            delay=DELAY,
            percent=PERCENT,
            duration=LATENCY_DURATION,
        )
        # A per-charm override may have changed the experiment's actual duration, so observe for
        # as long as the fault will actually run rather than the (possibly stale) caller value,
        # minus the time already spent resolving constraints and awaiting confirmed injection,
        # and detect if the fault stops or is removed before then.
        remaining = injection.duration.total_seconds() - (monotonic() - injection.injected_at)
        observe_io_latency(require_chaos_tool, target_model_ref, unit, VOLUME_PATH, max(0.0, remaining))
    except BaseException as error:
        observation_error = error
        raise
    finally:
        try:
            require_chaos_tool.cleanup_all()
        except BaseException as cleanup_error:
            if observation_error is not None:
                # Preserve the observation traceback and chain the cleanup failure.
                raise observation_error from cleanup_error
            raise

    # Recovery must happen without operator intervention.
    juju_client.multi_model_idle_for_period(models=models, timeout=RECOVER_TIMEOUT, strict_timeout=True)
    for model in models:
        juju_client.validate_model(model=model, level="deep")
