# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import shlex
from datetime import timedelta

from juju import JujuModelHandle
from kubernetes_client import KubernetesBackend

from .client import NativeChaosClient
from .kubernetes_client import KubernetesChaosClient


class DiskFillClient(NativeChaosClient):
    """Use native disk fill without falling back to workload-local stress-ng."""

    def fill_disk(self, model: JujuModelHandle, unit: str, path: str, size_mb: int) -> None:
        self._execute(model, unit, f"fallocate -l {size_mb}M -- {shlex.quote(path)}")

    def stress_cpu(self, model: JujuModelHandle, unit: str, workers: int, duration: timedelta) -> None:
        raise NotImplementedError

    def stress_memory(self, model: JujuModelHandle, unit: str, workers: int, size_mb: int, duration: timedelta) -> None:
        raise NotImplementedError

    def cleanup(self, model: JujuModelHandle, unit: str, path: str) -> None:
        # This adapter never starts stress-ng, so only remove its disk fill file.
        self._execute(model, unit, f"rm -f -- {shlex.quote(path)}")

    def _execute(self, model: JujuModelHandle, unit: str, command: str) -> None:
        result = self._juju.exec_unit(model, unit, command)
        if result.return_code != 0:
            raise RuntimeError(f"Disk fill command failed for {model.uri}/{unit}: exit code {result.return_code}.")


class NetworkIsolationClient(KubernetesChaosClient):
    """Only remove policies successfully created by this client."""

    def __init__(self, backend: KubernetesBackend) -> None:
        super().__init__(backend)
        self._isolated: set[tuple[str, str]] = set()

    def isolate_network(self, model: str, unit: str) -> None:
        super().isolate_network(model, unit)
        self._isolated.add((model, unit))

    def remove_network_isolation(self, model: str, unit: str) -> None:
        if (model, unit) not in self._isolated:
            return
        super().remove_network_isolation(model, unit)
        self._isolated.remove((model, unit))
