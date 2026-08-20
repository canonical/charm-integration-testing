# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

from juju import JujuBackend

from .backend import ChaosClient


class NativeChaosClient(ChaosClient):
    def __init__(self, juju_backend: JujuBackend):
        self._juju = juju_backend

    def fill_disk(self, model: str, unit: str, path: str, size_mb: int) -> None:
        self._juju.exec_unit(model, unit, f"fallocate -l {size_mb}M {path}")

    def stress_cpu(self, model: str, unit: str, workers: int, duration: timedelta) -> None:
        seconds = int(duration.total_seconds())
        self._juju.exec_unit(model, unit, f"stress-ng --cpu {workers} --timeout {seconds}s")

    def stress_memory(self, model: str, unit: str, workers: int, size_mb: int, duration: timedelta) -> None:
        seconds = int(duration.total_seconds())
        self._juju.exec_unit(model, unit, f"stress-ng --vm {workers} --vm-bytes {size_mb}M --timeout {seconds}s")

    def cleanup(self, model: str, unit: str, path: str) -> None:
        self._juju.exec_unit(model, unit, f"rm -f {path}")
        self._juju.exec_unit(model, unit, "pkill -f stress-ng || true")
