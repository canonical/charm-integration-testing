# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from chaos_client import NativeChaosClient
from juju import JujuBackend, JujuClient, JujuModelHandle, JujuWaitTimeoutError, is_agent_disconnected

from .scheduler.states import State

# Global default fill target. Per-charm overrides are tracked separately.
FILL_PERCENT = 98
FILL_FILE = "chaos-disk-fill.bin"
DETECT_TIMEOUT = timedelta(minutes=10)
# Disk fill recovers on the update-status hook interval, so allow a few cycles.
RECOVER_TIMEOUT = timedelta(minutes=15)


def _avail_mb_from_df(df_output: str) -> int:
    """Available MiB from `df -Pk` output (POSIX format: header row plus one data row)."""
    rows = [line for line in df_output.splitlines() if line.strip()]
    if len(rows) < 2 or len(rows[-1].split()) < 4:
        raise ValueError(f"unexpected df output: {df_output!r}")
    return int(rows[-1].split()[3]) // 1024


@pytest.mark.state(requires=State.DEPLOYED)
def test_live_disk_fill(
    juju_client: JujuClient,
    juju_backend: JujuBackend,
    native_chaos_client: NativeChaosClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
) -> None:
    unit = f"{target_application}/0"

    df = juju_backend.exec_unit(target_model_ref, unit, "df -Pk -- .")
    assert df.return_code == 0, f"df failed on {unit}: {df.stderr.strip()}"
    avail_mb = _avail_mb_from_df(df.stdout)
    fill_mb = avail_mb * FILL_PERCENT // 100
    assert fill_mb > 0, f"not enough free space on {unit} to run the disk-fill test (avail_mb={avail_mb})"

    try:
        native_chaos_client.fill_disk(model=target_model_ref, unit=unit, path=FILL_FILE, size_mb=fill_mb)
        created = juju_backend.exec_unit(target_model_ref, unit, f"test -s ./{FILL_FILE}")
        assert created.return_code == 0, f"disk fill file {FILL_FILE!r} was not created on {unit}"
        try:
            # Debounced against update-status blips; fails immediately if the Juju agent disconnects.
            juju_client.unhealthy_for_period(target_application, model=target_model_ref, timeout=DETECT_TIMEOUT)
        except JujuWaitTimeoutError as exc:
            if is_agent_disconnected(exc.wait_state):
                raise
            # Still active at timeout; validators are the ground truth for workload health.
            juju_client.validate_model(model=target_model_ref, level="deep")
    finally:
        native_chaos_client.cleanup(model=target_model_ref, unit=unit, path=FILL_FILE)
        removed = juju_backend.exec_unit(target_model_ref, unit, f"test ! -e ./{FILL_FILE}")
        assert removed.return_code == 0, f"disk fill file {FILL_FILE!r} was not removed from {unit} during cleanup"

    # Recovery must happen without operator intervention.
    juju_client.idle_for_period(model=target_model_ref, timeout=RECOVER_TIMEOUT)
    juju_client.validate_model(model=target_model_ref, level="simple")
