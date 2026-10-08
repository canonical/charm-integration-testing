# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from time import monotonic, sleep
from typing import Callable
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
    def __init__(
        self,
        backend: KubernetesBackend,
        *,
        startup_timeout: timedelta = timedelta(minutes=1),
        poll_interval: timedelta = timedelta(seconds=1),
        clock: Callable[[], float] = monotonic,
        pause: Callable[[float], None] = sleep,
    ) -> None:
        if min(startup_timeout.total_seconds(), poll_interval.total_seconds()) <= 0:
            raise ValueError("Startup timeout and poll interval must be positive.")
        self._startup_timeout = startup_timeout.total_seconds()
        self._poll_interval = poll_interval.total_seconds()
        self._clock = clock
        self._pause = pause
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

    def supports(self, operation: str) -> bool:
        match operation:
            case "stress_cpu" | "stress_memory":
                return CHAOS_MESH_CRDS[0] not in self._missing_crds
            case "io_latency":
                return CHAOS_MESH_CRDS[1] not in self._missing_crds
            case _:
                return False

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

    def check_stress(self, model: JujuModelHandle, unit: str, *, allow_completed: bool = False) -> None:
        """Validate tracked stress experiments during observation."""
        for plural, namespace, name in self._created:
            if plural != "stresschaos" or self._scopes[name] != (model.uri, unit, ""):
                continue
            current = self._backend.custom_objects_api.get_namespaced_custom_object(
                group=_GROUP, version=_VERSION, namespace=namespace, plural=plural, name=name, _request_timeout=30
            )
            metadata = current.get("metadata") or {}
            annotations = metadata.get("annotations") or {}
            if (
                not self._uids.get(name)
                or metadata.get("uid") != self._uids[name]
                or annotations.get(_OWNER_ANNOTATION) != self._owner
            ):
                raise RuntimeError(f"Cannot verify identity of StressChaos {namespace}/{name} during observation.")
            status = current.get("status") or {}
            experiment = status.get("experiment") or {}
            conditions = {item["type"]: item.get("status") for item in status.get("conditions") or []}
            records = experiment.get("containerRecords") or []
            # Controller failures are recorded per container, not as a global Error phase.
            failures = [
                event for record in records for event in record.get("events") or [] if event.get("type") == "Failed"
            ]
            if failures:
                raise RuntimeError(f"StressChaos {namespace}/{name} failed during observation: {failures}")
            if not records:
                raise RuntimeError(f"StressChaos {namespace}/{name} has no container records during observation.")
            if (
                metadata.get("deletionTimestamp")
                or annotations.get("experiment.chaos-mesh.org/pause") == "true"
                or conditions.get("Paused") == "True"
            ):
                raise RuntimeError(f"StressChaos {namespace}/{name} was interrupted during observation.")
            if (
                allow_completed
                and experiment.get("desiredPhase") == "Stop"
                and conditions.get("AllRecovered") == "True"
                and all(record.get("phase") == "Not Injected" for record in records)
            ):
                continue
            if not (
                experiment.get("desiredPhase") == "Run"
                and conditions.get("Selected") == "True"
                and conditions.get("AllInjected") == "True"
                and conditions.get("AllRecovered") in (None, "False")
                and all(record.get("phase") == "Injected" for record in records)
            ):
                raise RuntimeError(f"StressChaos {namespace}/{name} is no longer confirmed active: {status}")

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
        if "stresschaos.chaos-mesh.org" in self._missing_crds:
            raise NotImplementedError("StressChaos experiments require the 'stresschaos.chaos-mesh.org' CRD.")
        pods = self._backend.core_v1_api.list_namespaced_pod(
            namespace=model.model,
            label_selector=f"app.kubernetes.io/name={unit.split('/')[0]}",
            _request_timeout=30,
        )
        matches = [
            pod
            for pod in pods.items
            if (pod.metadata.annotations or {}).get("unit.juju.is/id") == unit
            and pod.metadata.deletion_timestamp is None
            and (pod.status is None or pod.status.phase not in {"Succeeded", "Failed"})
        ]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one live Pod for {model.model}/{unit}, found {len(matches)}.")
        application = unit.split("/")[0]
        spec: dict[str, object] = {
            "mode": "all",
            "selector": {"pods": {model.model: [matches[0].metadata.name]}},
            "stressors": stressors,
            "duration": f"{int(duration.total_seconds())}s",
        }
        if "memory" in stressors:
            containers = [
                item.name
                for item in (matches[0].spec.containers if matches[0].spec else [])
                if any(env.name == "JUJU_CONTAINER_NAME" and env.value == item.name for env in item.env or [])
            ]
            if len(containers) != 1:
                raise RuntimeError(f"Memory stress requires one workload container, found {containers}.")
            spec["containerNames"] = containers
        name = self._name(label, application)
        self._create("StressChaos", "stresschaos", model, unit, "", name, spec)
        self._wait_for_stress_injection(model.model, name)

    def _wait_for_stress_injection(self, namespace: str, name: str) -> None:
        """Wait for controller-confirmed injection, retaining failed runs for cleanup."""
        deadline = self._clock() + self._startup_timeout
        while (remaining := deadline - self._clock()) > 0:
            current = self._backend.custom_objects_api.get_namespaced_custom_object(
                group=_GROUP,
                version=_VERSION,
                namespace=namespace,
                plural="stresschaos",
                name=name,
                _request_timeout=min(30, remaining),
            )
            metadata = current.get("metadata") or {}
            if (
                not self._uids.get(name)
                or metadata.get("uid") != self._uids[name]
                or (metadata.get("annotations") or {}).get(_OWNER_ANNOTATION) != self._owner
            ):
                raise RuntimeError(f"Cannot verify identity of StressChaos {namespace}/{name} during injection.")
            status = current.get("status") or {}
            experiment = status.get("experiment") or {}
            conditions = {item["type"]: item.get("status") for item in status.get("conditions") or []}
            if (
                metadata.get("deletionTimestamp")
                or experiment.get("desiredPhase") == "Stop"
                or conditions.get("Paused") == "True"
            ):
                raise RuntimeError(f"StressChaos {namespace}/{name} stopped before injection was confirmed.")
            if (
                conditions.get("Selected") == "True"
                and conditions.get("AllInjected") == "True"
                and experiment.get("desiredPhase") == "Run"
                and conditions.get("AllRecovered") in (None, "False")
            ):
                if self._clock() < deadline:
                    return
                break
            remaining = deadline - self._clock()
            if remaining > 0:
                self._pause(min(self._poll_interval, remaining))
        raise TimeoutError(f"Timed out waiting for StressChaos {namespace}/{name} injection.")

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
