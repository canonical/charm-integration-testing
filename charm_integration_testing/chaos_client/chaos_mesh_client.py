# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from time import monotonic, sleep
from typing import Any, Callable
from uuid import uuid4

from juju import JujuModelHandle
from kubernetes.client import ApiException, V1DeleteOptions, V1Preconditions  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

from .backend import ChaosClient
from .chaos_mesh_detection import CHAOS_MESH_CRDS, missing_chaos_mesh_crds
from .target import workload_target

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
        cleanup_timeout: timedelta = timedelta(minutes=1),
        poll_interval: timedelta = timedelta(seconds=1),
        request_timeout: timedelta = timedelta(seconds=30),
        clock: Callable[[], float] = monotonic,
        pause: Callable[[float], None] = sleep,
    ) -> None:
        if min(startup_timeout, cleanup_timeout, poll_interval, request_timeout) <= timedelta(0):
            raise ValueError("Startup, cleanup and request timeouts and poll interval must be positive.")
        self._startup_timeout = startup_timeout.total_seconds()
        self._cleanup_timeout = cleanup_timeout.total_seconds()
        self._poll_interval = poll_interval.total_seconds()
        self._request_timeout = request_timeout.total_seconds()
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
        self._cpu_stress: set[str] = set()
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
        if not self.supports("stress_cpu"):
            raise NotImplementedError("StressChaos experiments require the 'stresschaos.chaos-mesh.org' CRD.")
        pod, container = workload_target(self._backend, model.model, unit, request_timeout=self._request_timeout)
        name = self._name("cpu-stress", unit.split("/")[0])
        spec: dict[str, object] = {
            "mode": "all",
            "selector": {"pods": {model.model: [pod]}},
            "containerNames": [container],
            "stressors": {"cpu": {"workers": workers}},
            "duration": f"{int(duration.total_seconds())}s",
        }
        self._create("StressChaos", "stresschaos", model, unit, "", name, spec)
        self._cpu_stress.add(name)
        self._wait(
            lambda timeout: self._stress_injected(model.model, name, timeout),
            self._clock() + self._startup_timeout,
            name,
            "injection",
        )

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
            deadline = self._clock() + self._cleanup_timeout
            current = self._read_resource(plural, namespace, name, self._request_budget(deadline))
            try:
                if current is not None:
                    self._backend.custom_objects_api.delete_namespaced_custom_object(
                        group=_GROUP,
                        version=_VERSION,
                        namespace=namespace,
                        plural=plural,
                        name=name,
                        body=V1DeleteOptions(preconditions=V1Preconditions(uid=self._uids[name])),
                        _request_timeout=self._request_budget(deadline),
                    )
            except ApiException as error:
                if error.status != 404:
                    raise
            if name in self._cpu_stress:
                self._wait(
                    lambda timeout: self._read_resource(plural, namespace, name, timeout) is None,
                    deadline,
                    name,
                    "deletion",
                )
            self._created.remove(resource)
            del self._scopes[name]
            self._uids.pop(name, None)
            self._cpu_stress.discard(name)

    def check_stress(self, model: JujuModelHandle, unit: str) -> None:
        """Require controller-confirmed active injection for every tracked stress resource."""
        resources = [
            (namespace, name)
            for plural, namespace, name in self._created
            if plural == "stresschaos" and self._scopes[name] == (model.uri, unit, "")
        ]
        if not resources:
            raise RuntimeError(f"No tracked Chaos Mesh stress for {model.uri}/{unit}.")
        for namespace, name in resources:
            if not self._stress_injected(namespace, name, self._request_timeout):
                raise RuntimeError(f"StressChaos {namespace}/{name} is no longer confirmed active.")

    def _read_resource(self, plural: str, namespace: str, name: str, timeout: float) -> dict[str, Any] | None:
        try:
            current: dict[str, Any] = self._backend.custom_objects_api.get_namespaced_custom_object(
                group=_GROUP,
                version=_VERSION,
                namespace=namespace,
                plural=plural,
                name=name,
                _request_timeout=timeout,
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise
        metadata = current.get("metadata") or {}
        if (metadata.get("annotations") or {}).get(_OWNER_ANNOTATION) != self._owner:
            raise RuntimeError(f"Cannot verify ownership of {plural} {namespace}/{name}.")
        if not self._uids.get(name):
            raise RuntimeError(f"Creation UID was not recorded for {plural} {namespace}/{name}; cleanup retained.")
        if metadata.get("uid") != self._uids[name]:
            raise RuntimeError(f"Cannot verify UID of {plural} {namespace}/{name}.")
        return current

    def _stress_injected(self, namespace: str, name: str, timeout: float) -> bool:
        current = self._read_resource("stresschaos", namespace, name, timeout)
        if current is None:
            raise RuntimeError(f"StressChaos {namespace}/{name} disappeared during stress.")
        metadata = current.get("metadata") or {}
        status = current.get("status") or {}
        experiment = status.get("experiment") or {}
        conditions = {item["type"]: item.get("status") for item in status.get("conditions") or []}
        records = experiment.get("containerRecords") or []
        failures = [
            event for record in records for event in record.get("events") or [] if event.get("type") == "Failed"
        ]
        if failures:
            raise RuntimeError(f"StressChaos {namespace}/{name} failed: {failures}")
        if (
            metadata.get("deletionTimestamp")
            or (metadata.get("annotations") or {}).get("experiment.chaos-mesh.org/pause") == "true"
            or conditions.get("Paused") == "True"
            or experiment.get("desiredPhase") == "Stop"
        ):
            raise RuntimeError(f"StressChaos {namespace}/{name} stopped or was interrupted during stress.")
        return (
            experiment.get("desiredPhase") == "Run"
            and conditions.get("Selected") == "True"
            and conditions.get("AllInjected") == "True"
            and conditions.get("AllRecovered") == "False"
            and bool(records)
            and all(record.get("phase") == "Injected" for record in records)
        )

    def _request_budget(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("Chaos Mesh operation timed out.")
        return min(self._request_timeout, remaining)

    def _wait(self, check: Callable[[float], bool], deadline: float, name: str, stage: str) -> None:
        while self._clock() < deadline:
            complete = check(self._request_budget(deadline))
            if self._clock() >= deadline:
                break
            if complete:
                return
            self._pause(min(self._poll_interval, max(0, deadline - self._clock())))
        raise TimeoutError(f"Chaos Mesh {stage} timed out for {name}.")

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
        name = self._name(label, application)
        self._create("StressChaos", "stresschaos", model, unit, "", name, spec)

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
                group=_GROUP,
                version=_VERSION,
                namespace=namespace,
                plural=plural,
                body=body,
                _request_timeout=self._request_timeout,
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
