# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from uuid import uuid4

from juju import JujuModelHandle
from kubernetes.client import ApiException, V1DeleteOptions, V1Preconditions  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

from .backend import ChaosClient
from .chaos_mesh_detection import CHAOS_MESH_CRDS, missing_chaos_mesh_crds

_GROUP = "chaos-mesh.org"
_VERSION = "v1alpha1"
_OWNER_ANNOTATION = "charm-integration-testing/owner"


class ChaosMeshNotInstalledError(RuntimeError):
    """Raised when a ChaosMeshChaosClient is constructed against a cluster without Chaos Mesh."""


class ChaosMeshChaosClient(ChaosClient):
    def __init__(self, backend: KubernetesBackend):
        missing = missing_chaos_mesh_crds(backend)
        if len(missing) == len(CHAOS_MESH_CRDS):
            raise ChaosMeshNotInstalledError(
                f"Chaos Mesh is not fully installed on the target cluster (CRDs absent: {', '.join(missing)})."
            )
        self._backend = backend
        self._owner = uuid4().hex
        self._uids: dict[str, str] = {}
        self._missing_crds = frozenset(missing)
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
                current = self._backend.custom_objects_api.get_namespaced_custom_object(
                    group=_GROUP, version=_VERSION, namespace=namespace, plural=plural, name=name
                )
                metadata = current.get("metadata") or {}
                if (metadata.get("annotations") or {}).get(_OWNER_ANNOTATION) != self._owner:
                    raise RuntimeError(f"Cannot verify ownership of {plural} {namespace}/{name}.")
                uid = metadata.get("uid")
                if not self._uids.get(name):
                    raise RuntimeError(
                        f"Creation UID was not recorded for {plural} {namespace}/{name}; cleanup retained."
                    )
                if not uid or self._uids[name] != uid:
                    raise RuntimeError(f"Cannot verify UID of {plural} {namespace}/{name}.")
                self._backend.custom_objects_api.delete_namespaced_custom_object(
                    group=_GROUP,
                    version=_VERSION,
                    namespace=namespace,
                    plural=plural,
                    name=name,
                    body=V1DeleteOptions(preconditions=V1Preconditions(uid=uid)),
                )
            except ApiException as error:
                if error.status != 404:
                    raise
            self._created.remove(resource)
            del self._scopes[name]
            self._uids.pop(name, None)

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
        crd = f"{plural}.{_GROUP}"
        if crd in self._missing_crds:
            raise NotImplementedError(f"{kind} experiments require the '{crd}' CRD.")
        namespace = model.model
        body: dict[str, object] = {
            "apiVersion": f"{_GROUP}/{_VERSION}",
            "kind": kind,
            "metadata": {"name": name, "namespace": namespace, "annotations": {_OWNER_ANNOTATION: self._owner}},
            "spec": spec,
        }
        # Track before POST because the resource may exist even if the response is lost.
        resource = (plural, namespace, name)
        self._created.append(resource)
        self._scopes[name] = (model.uri, unit, path)
        try:
            created = self._backend.custom_objects_api.create_namespaced_custom_object(
                group=_GROUP, version=_VERSION, namespace=namespace, plural=plural, body=body
            )
            uid = (created.get("metadata") or {}).get("uid")
            if uid:
                self._uids[name] = uid
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
