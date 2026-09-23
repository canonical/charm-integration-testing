# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import shlex
from datetime import timedelta
from uuid import uuid4

from juju import JujuModelHandle
from kubernetes import client  # type: ignore[import-untyped]
from kubernetes.client import ApiException  # type: ignore[import-untyped]
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
    """Track policy ownership even when creation returns an ambiguous error."""

    def __init__(self, backend: KubernetesBackend) -> None:
        super().__init__(backend)
        self._owner = uuid4().hex
        self._isolated: dict[tuple[str, str], str] = {}

    def isolate_network(self, model: str, unit: str) -> None:
        policy = self._network_policy(model, unit)
        policy.metadata.annotations = {"charm-integration-testing/owner": self._owner}
        self._isolated[(model, unit)] = policy.metadata.name
        self._backend.networking_v1_api.create_namespaced_network_policy(namespace=model, body=policy)

    def remove_network_isolation(self, model: str, unit: str) -> None:
        name = self._isolated.get((model, unit))
        if name is None:
            return
        api = self._backend.networking_v1_api
        try:
            policy = api.read_namespaced_network_policy(name=name, namespace=model)
            annotations = policy.metadata.annotations or {}
            if annotations.get("charm-integration-testing/owner") == self._owner:
                if not policy.metadata.uid:
                    raise RuntimeError(f"Cannot safely delete NetworkPolicy {model}/{name} without its UID.")
                # Do not delete a replacement created between the read and delete.
                api.delete_namespaced_network_policy(
                    name=name,
                    namespace=model,
                    body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=policy.metadata.uid)),
                )
        except ApiException as error:
            if error.status != 404:
                raise
        del self._isolated[(model, unit)]
