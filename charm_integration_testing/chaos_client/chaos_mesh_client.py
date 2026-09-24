# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from uuid import uuid4

from juju import JujuModelHandle
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

from .backend import ChaosClient

_GROUP = "chaos-mesh.org"
_VERSION = "v1alpha1"
_REQUIRED_CRDS = ("stresschaos.chaos-mesh.org", "iochaos.chaos-mesh.org")


class ChaosMeshNotInstalledError(RuntimeError):
    """Raised when a ChaosMeshChaosClient is constructed against a cluster without Chaos Mesh."""


class ChaosMeshChaosClient(ChaosClient):
    def __init__(self, backend: KubernetesBackend):
        missing = [crd for crd in _REQUIRED_CRDS if not backend.crd_exists(crd)]
        if missing:
            raise ChaosMeshNotInstalledError(
                f"Chaos Mesh is not fully installed on the target cluster (CRDs absent: {', '.join(missing)})."
            )
        self._backend = backend
        self._scopes: dict[str, tuple[str, str, str]] = {}
        self._created: list[tuple[str, str, str]] = []  # (plural, namespace, name)

    def stress_cpu(self, model: JujuModelHandle, unit: str, workers: int, duration: timedelta) -> None:
        self._create_stress_chaos(model, unit, "cpu-stress", {"cpu": {"workers": workers}}, duration)

    def stress_memory(self, model: JujuModelHandle, unit: str, workers: int, size_mb: int, duration: timedelta) -> None:
        self._create_stress_chaos(
            model, unit, "memory-stress", {"memory": {"workers": workers, "size": f"{size_mb}MB"}}, duration
        )

    def io_latency(
        self,
        model: JujuModelHandle,
        unit: str,
        volume_path: str,
        delay: timedelta,
        percent: int,
        duration: timedelta,
    ) -> None:
        application = unit.split("/")[0]
        spec: dict[str, object] = {
            "action": "latency",
            "mode": "all",
            "selector": self._selector(model.model, application),
            "volumePath": volume_path,
            "delay": f"{int(delay.total_seconds() * 1000)}ms",
            "percent": percent,
            "duration": f"{int(duration.total_seconds())}s",
        }
        self._create("IOChaos", "iochaos", model, unit, volume_path, self._name("io-latency", application), spec)

    def cleanup(self, model: JujuModelHandle, unit: str, path: str) -> None:
        """Clean resources for the model, unit and path. An empty path selects stress."""
        for resource in reversed(tuple(self._created)):
            plural, namespace, name = resource
            if self._scopes[name] != (model.uri, unit, path):
                continue
            try:
                self._backend.custom_objects_api.delete_namespaced_custom_object(
                    group=_GROUP, version=_VERSION, namespace=namespace, plural=plural, name=name
                )
            except ApiException as error:
                if error.status != 404:
                    raise
            self._created.remove(resource)
            del self._scopes[name]

    def fill_disk(self, model: JujuModelHandle, unit: str, path: str, size_mb: int) -> None:
        raise NotImplementedError

    def isolate_network(self, model: str, unit: str) -> None:
        raise NotImplementedError

    def remove_network_isolation(self, model: str, unit: str) -> None:
        raise NotImplementedError

    def _create_stress_chaos(
        self,
        model: JujuModelHandle,
        unit: str,
        label: str,
        stressors: dict[str, object],
        duration: timedelta,
    ) -> None:
        application = unit.split("/")[0]
        spec: dict[str, object] = {
            "mode": "all",
            "selector": self._selector(model.model, application),
            "stressors": stressors,
            "duration": f"{int(duration.total_seconds())}s",
        }
        self._create("StressChaos", "stresschaos", model, unit, "", self._name(label, application), spec)

    def _create(
        self, kind: str, plural: str, model: JujuModelHandle, unit: str, path: str, name: str, spec: dict[str, object]
    ) -> None:
        namespace = model.model
        body: dict[str, object] = {
            "apiVersion": f"{_GROUP}/{_VERSION}",
            "kind": kind,
            "metadata": {"name": name, "namespace": namespace},
            "spec": spec,
        }
        # Track before POST because the resource may exist even if the response is lost.
        resource = (plural, namespace, name)
        self._created.append(resource)
        self._scopes[name] = (model.uri, unit, path)
        try:
            self._backend.custom_objects_api.create_namespaced_custom_object(
                group=_GROUP, version=_VERSION, namespace=namespace, plural=plural, body=body
            )
        except ApiException as error:
            if error.status == 409:
                # A conflicting resource belongs to an earlier create request.
                self._created.remove(resource)
                del self._scopes[name]
            raise

    @staticmethod
    def _selector(namespace: str, application: str) -> dict[str, object]:
        return {
            "namespaces": [namespace],
            "labelSelectors": {"app.kubernetes.io/name": application},
        }

    @staticmethod
    def _name(label: str, application: str) -> str:
        return f"chaos-{label}-{application}-{uuid4().hex[:8]}"
