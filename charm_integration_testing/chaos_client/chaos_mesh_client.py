# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from time import monotonic, sleep
from typing import Any, Callable
from uuid import uuid4

from juju import JujuModelHandle
from kubernetes.client import ApiException, V1DeleteOptions, V1Preconditions  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

from .backend import ChaosClient, IoLatencyInjection
from .chaos_mesh_detection import CHAOS_MESH_CRDS, missing_chaos_mesh_crds
from .target import unit_pod_name, workload_target

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
        cleanup_timeout: timedelta = timedelta(minutes=2),
        poll_interval: timedelta = timedelta(seconds=1),
        request_timeout: timedelta = timedelta(seconds=30),
        clock: Callable[[], float] = monotonic,
        pause: Callable[[float], None] = sleep,
    ) -> None:
        if min(startup_timeout, cleanup_timeout, poll_interval, request_timeout) <= timedelta(0):
            raise ValueError("Startup, cleanup and request timeouts and poll interval must be positive.")
        self._memory_stress: set[str] = set()
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
    ) -> IoLatencyInjection:
        if "iochaos.chaos-mesh.org" in self._missing_crds:
            raise NotImplementedError("IOChaos experiments require the 'iochaos.chaos-mesh.org' CRD.")
        application = unit.split("/")[0]
        pod = unit_pod_name(self._backend, model.model, unit, request_timeout=self._request_timeout)
        spec: dict[str, object] = {
            "action": "latency",
            "mode": "all",
            "selector": {"pods": {model.model: [pod]}},
            "volumePath": volume_path,
            "delay": f"{int(delay.total_seconds() * 1000)}ms",
            "percent": percent,
            "duration": f"{int(duration.total_seconds())}s",
        }
        name = self._name("io-latency", application)
        self._create("IOChaos", "iochaos", model, unit, volume_path, name, spec)
        # Sampled right after the POST returns: Chaos Mesh's controller starts counting down
        # the duration from the resource's actual creation, so this is the closest available
        # approximation. Sampling before submitting would miss however long the POST itself
        # took, making the reported start earlier than the real one and letting callers end
        # observation (and clean up) before the configured duration has actually elapsed.
        injected_at = self._clock()
        self._wait(
            lambda timeout: self._experiment_injected("iochaos", "IOChaos", model.model, name, timeout),
            self._clock() + self._startup_timeout,
            name,
            "injection",
        )
        return IoLatencyInjection(duration=duration, injected_at=injected_at)

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
            if name in self._cpu_stress or name in self._memory_stress:
                self._wait(
                    lambda timeout: self._read_resource(plural, namespace, name, timeout) is None,
                    deadline,
                    name,
                    "deletion",
                )
            self._memory_stress.discard(name)
            self._created.remove(resource)
            del self._scopes[name]
            self._uids.pop(name, None)
            self._cpu_stress.discard(name)

    def check_stress(self, model: JujuModelHandle, unit: str, *, allow_completed: bool = False) -> None:
        """Require active injection or explicitly allowed normal completion for tracked stress."""
        resources = [
            (namespace, name)
            for plural, namespace, name in self._created
            if plural == "stresschaos" and self._scopes[name] == (model.uri, unit, "")
        ]
        if not resources:
            raise RuntimeError(f"No tracked Chaos Mesh stress for {model.uri}/{unit}.")
        for namespace, name in resources:
            if not self._experiment_injected(
                "stresschaos", "StressChaos", namespace, name, self._request_timeout, allow_completed=allow_completed
            ):
                raise RuntimeError(f"StressChaos {namespace}/{name} is no longer confirmed active.")

    def check_io_latency(
        self, model: JujuModelHandle, unit: str, volume_path: str, *, allow_completed: bool = False
    ) -> None:
        """Require active injection or explicitly allowed normal completion for tracked I/O latency."""
        resources = [
            (namespace, name)
            for plural, namespace, name in self._created
            if plural == "iochaos" and self._scopes[name] == (model.uri, unit, volume_path)
        ]
        if not resources:
            raise RuntimeError(f"No tracked Chaos Mesh I/O latency for {model.uri}/{unit}:{volume_path}.")
        for namespace, name in resources:
            if not self._experiment_injected(
                "iochaos", "IOChaos", namespace, name, self._request_timeout, allow_completed=allow_completed
            ):
                raise RuntimeError(f"IOChaos {namespace}/{name} is no longer confirmed active.")

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

    def _experiment_injected(
        self, plural: str, kind: str, namespace: str, name: str, timeout: float, *, allow_completed: bool = False
    ) -> bool:
        current = self._read_resource(plural, namespace, name, timeout)
        if current is None:
            raise RuntimeError(f"{kind} {namespace}/{name} disappeared during observation.")
        metadata = current.get("metadata") or {}
        status = current.get("status") or {}
        experiment = status.get("experiment") or {}
        conditions = {item["type"]: item.get("status") for item in status.get("conditions") or []}
        records = experiment.get("containerRecords") or []
        failures = [
            event for record in records for event in record.get("events") or [] if event.get("type") == "Failed"
        ]
        if failures:
            raise RuntimeError(f"{kind} {namespace}/{name} failed: {failures}")
        if (
            metadata.get("deletionTimestamp")
            or (metadata.get("annotations") or {}).get("experiment.chaos-mesh.org/pause") == "true"
            or conditions.get("Paused") == "True"
        ):
            raise RuntimeError(f"{kind} {namespace}/{name} stopped or was interrupted during observation.")
        if experiment.get("desiredPhase") == "Stop":
            if (
                allow_completed
                and conditions.get("AllRecovered") == "True"
                and records
                and all(record.get("phase") == "Not Injected" for record in records)
            ):
                return True
            raise RuntimeError(f"{kind} {namespace}/{name} stopped or was interrupted during observation.")
        return (
            experiment.get("desiredPhase") == "Run"
            and conditions.get("Selected") == "True"
            and conditions.get("AllInjected") == "True"
            and conditions.get("AllRecovered") in (None, "False")
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
        if "stresschaos.chaos-mesh.org" in self._missing_crds:
            raise NotImplementedError("StressChaos experiments require the 'stresschaos.chaos-mesh.org' CRD.")
        pod, container = workload_target(self._backend, model.model, unit, request_timeout=self._request_timeout)
        spec: dict[str, object] = {
            "mode": "all",
            "selector": {"pods": {model.model: [pod]}},
            "containerNames": [container],
            "stressors": stressors,
            "duration": f"{int(duration.total_seconds())}s",
        }
        name = self._name(label, unit.split("/")[0])
        tracked = self._memory_stress if "memory" in stressors else self._cpu_stress
        tracked.add(name)
        self._create("StressChaos", "stresschaos", model, unit, "", name, spec)
        self._wait(
            lambda timeout: self._experiment_injected("stresschaos", "StressChaos", model.model, name, timeout),
            self._clock() + self._startup_timeout,
            name,
            "injection",
        )

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
                self._memory_stress.discard(name)
                self._created.remove(resource)
                del self._scopes[name]
                self._cpu_stress.discard(name)
            raise

    @staticmethod
    def _name(label: str, application: str) -> str:
        return f"chaos-{label}-{application}-{uuid4().hex[:8]}"
