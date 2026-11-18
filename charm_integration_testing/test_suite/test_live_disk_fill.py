# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import shlex
from datetime import timedelta
from pathlib import PurePosixPath
from time import sleep
from uuid import uuid4

import pytest
from chaos_client import ChaosCleanupError, MetaChaosClient
from juju import JujuBackend, JujuClient, JujuModelHandle

from .scheduler.states import State

# Per-charm disk fill sizes override this default percentage of available space.
FILL_PERCENT = 98
FILL_DURATION = timedelta(minutes=10)
# Disk fill recovers on the update-status hook interval, so allow a few cycles.
RECOVER_TIMEOUT = timedelta(minutes=15)
logger = logging.getLogger(__name__)


def _storage_paths(backend: JujuBackend, model: JujuModelHandle, unit: str) -> list[str]:
    """Find the filesystems attached to the unit through Juju storage."""
    result = backend.exec_unit(model, unit, "storage-list --format=json")
    assert result.return_code == 0, f"storage-list failed on {unit}: {result.stderr.strip()}"
    storage_ids = json.loads(result.stdout)
    if not isinstance(storage_ids, list) or not all(isinstance(item, str) for item in storage_ids):
        raise ValueError(f"Unexpected storage-list output on {unit}: {result.stdout!r}")
    paths = []
    devices = set()
    for storage_id in storage_ids:
        result = backend.exec_unit(model, unit, f"storage-get -s {shlex.quote(storage_id)} --format=json")
        assert result.return_code == 0, f"storage-get failed for {storage_id}: {result.stderr.strip()}"
        storage = json.loads(result.stdout)
        if storage.get("kind") != "filesystem":
            raise ValueError(f"Disk fill requires filesystem storage, got {storage_id}: {storage!r}")
        path = storage.get("location", "")
        if not isinstance(path, str) or not PurePosixPath(path).is_absolute() or path == "/":
            raise ValueError(f"Invalid storage location for {storage_id}: {path!r}")
        quoted_path = shlex.quote(path)
        result = backend.exec_unit(model, unit, f"mountpoint -q -- {quoted_path} && stat -c %d -- {quoted_path}")
        assert result.return_code == 0, f"Storage {storage_id} is not mounted at {path!r} on {unit}"
        device = int(result.stdout.strip())
        logger.info("Disk fill storage %s on %s is mounted at %s", storage_id, unit, path)
        if device not in devices:
            devices.add(device)
            paths.append(path)
    return paths


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
    paths = _storage_paths(juju_backend, target_model_ref, unit)
    if not paths:
        pytest.skip(f"No persistent storage attached to {unit}")
    fill_files: list[str] = []
    try:
        for path in paths:
            df = juju_backend.exec_unit(target_model_ref, unit, f"df -Pk -- {shlex.quote(path)}")
            assert df.return_code == 0, f"df failed on {unit} at {path}: {df.stderr.strip()}"
            avail_mb = _avail_mb_from_df(df.stdout)
            fill_mb = avail_mb * FILL_PERCENT // 100
            assert fill_mb > 0, f"not enough free space on {unit} at {path} (avail_mb={avail_mb})"
            fill_file = str(PurePosixPath(path) / f"chaos-disk-fill-{uuid4().hex}.bin")
            # Allocation may create a partial file before failing.
            fill_files.append(fill_file)
            logger.info("Requesting disk fill at %s on %s with %s MiB", path, unit, fill_mb)
            require_chaos_tool.fill_disk(model=target_model_ref, unit=unit, path=fill_file, size_mb=fill_mb)
            created = juju_backend.exec_unit(target_model_ref, unit, f"test -s {shlex.quote(fill_file)}")
            assert created.return_code == 0, f"disk fill file {fill_file!r} was not created on {unit}"
        # A healthy workload may remain active throughout the fault.
        sleep(FILL_DURATION.total_seconds())
    finally:
        errors: list[Exception] = []
        for fill_file in reversed(fill_files):
            try:
                require_chaos_tool.cleanup(model=target_model_ref, unit=unit, path=fill_file)
                removed = juju_backend.exec_unit(target_model_ref, unit, f"test ! -e {shlex.quote(fill_file)}")
                assert (
                    removed.return_code == 0
                ), f"disk fill file {fill_file!r} was not removed from {unit} during cleanup"
                logger.info("Removed disk fill file %s from %s", fill_file, unit)
            except Exception as error:
                errors.append(error)
        if errors:
            raise ChaosCleanupError(errors) from errors[0]

    # Recovery must happen without operator intervention.
    juju_client.multi_model_idle_for_period(models=models, timeout=RECOVER_TIMEOUT, strict_timeout=True)
    for model in models:
        juju_client.validate_model(model=model, level="deep")
