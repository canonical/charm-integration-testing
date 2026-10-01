# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from time import sleep
from uuid import uuid4

import pytest
from chaos_client import MetaChaosClient
from juju import JujuBackend, JujuClient, JujuModelHandle

from .scheduler.states import State

# Global default fill target. Per-charm overrides are tracked separately.
FILL_PERCENT = 98
FILL_DURATION = timedelta(minutes=10)
# Disk fill recovers on the update-status hook interval, so allow a few cycles.
RECOVER_TIMEOUT = timedelta(minutes=15)


def _avail_mb_from_df(df_output: str) -> int:
    """Available MiB from `df -Pk` output (POSIX format: header row plus one data row)."""
    rows = [line for line in df_output.splitlines() if line.strip()]
    if len(rows) < 2 or len(rows[-1].split()) < 4:
        raise ValueError(f"unexpected df output: {df_output!r}")
    return int(rows[-1].split()[3]) // 1024


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_live_disk_fill(
    juju_client: JujuClient,
    juju_backend: JujuBackend,
    require_chaos_tool: MetaChaosClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    neighbor_model_ref: JujuModelHandle | None,
) -> None:
    models = list(dict.fromkeys([target_model_ref, *([neighbor_model_ref] if neighbor_model_ref else [])]))
    juju_client.multi_model_idle_for_period(models=models, timeout=RECOVER_TIMEOUT, strict_timeout=True)
    unit = f"{target_application}/0"
    fill_file = f"chaos-disk-fill-{uuid4().hex}.bin"

    df = juju_backend.exec_unit(target_model_ref, unit, "df -Pk -- .")
    assert df.return_code == 0, f"df failed on {unit}: {df.stderr.strip()}"
    avail_mb = _avail_mb_from_df(df.stdout)
    fill_mb = avail_mb * FILL_PERCENT // 100
    assert fill_mb > 0, f"not enough free space on {unit} to run the disk-fill test (avail_mb={avail_mb})"

    try:
        require_chaos_tool.fill_disk(model=target_model_ref, unit=unit, path=fill_file, size_mb=fill_mb)
        created = juju_backend.exec_unit(target_model_ref, unit, f"test -s ./{fill_file}")
        assert created.return_code == 0, f"disk fill file {fill_file!r} was not created on {unit}"
        # A healthy workload may remain active throughout the fault.
        sleep(FILL_DURATION.total_seconds())
    finally:
        require_chaos_tool.cleanup(model=target_model_ref, unit=unit, path=fill_file)
        removed = juju_backend.exec_unit(target_model_ref, unit, f"test ! -e ./{fill_file}")
        assert removed.return_code == 0, f"disk fill file {fill_file!r} was not removed from {unit} during cleanup"

    # Recovery must happen without operator intervention.
    juju_client.multi_model_idle_for_period(models=models, timeout=RECOVER_TIMEOUT, strict_timeout=True)
    for model in models:
        juju_client.validate_model(model=model, level="deep")
