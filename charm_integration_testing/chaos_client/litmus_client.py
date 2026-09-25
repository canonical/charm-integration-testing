# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from datetime import timedelta
from time import monotonic, sleep
from typing import Any, Callable
from uuid import uuid4

from juju import JujuModelHandle
from kubernetes import client  # type: ignore[import-untyped]
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

from .backend import ChaosClient
from .litmus_detection import LITMUS_CRDS
from .litmus_experiments import RUNNER_IMAGE, TERMINATION_GRACE_SECONDS, LitmusExperiment
from .litmus_setup import OWNER_ANNOTATION, REQUEST_TIMEOUT, LitmusSetup
from .meta_client import ChaosCleanupError

_GROUP = "litmuschaos.io"
_VERSION = "v1alpha1"


class LitmusNotInstalledError(RuntimeError):
    """Raised when required Litmus CRDs are absent."""


@dataclass
class _ExperimentRun:
    scope: tuple[str, str]
    namespace: str
    name: str
    setup: LitmusSetup
    pod: str
    injected: bool = False
    reverted: bool = False
    stop_requested: bool = False
    execution_error: RuntimeError | None = None
    engine_requested: bool = False
    uid: str | None = None
    cleanup_uids: dict[tuple[str, str], str] = field(default_factory=dict)


class LitmusChaosClient(ChaosClient):
    """Prepare, execute and clean up Litmus stress experiments."""

    def __init__(
        self,
        backend: KubernetesBackend,
        *,
        target_container: str | None = None,
        startup_timeout: timedelta = timedelta(minutes=5),
        cleanup_timeout: timedelta = timedelta(minutes=5),
        poll_interval: timedelta = timedelta(seconds=1),
        clock: Callable[[], float] = monotonic,
        pause: Callable[[float], None] = sleep,
    ) -> None:
        missing = [crd for crd in LITMUS_CRDS if not backend.crd_exists(crd)]
        if missing:
            raise LitmusNotInstalledError(f"Litmus CRDs absent: {', '.join(missing)}.")
        if min(startup_timeout.total_seconds(), cleanup_timeout.total_seconds(), poll_interval.total_seconds()) <= 0:
            raise ValueError("Startup timeout, cleanup timeout and poll interval must be positive.")
        self._backend = backend
        self._batch = client.BatchV1Api(backend.api_client)
        self._target_container = target_container
        self._startup_timeout = startup_timeout.total_seconds()
        self._cleanup_timeout = cleanup_timeout.total_seconds()
        self._poll_interval = poll_interval.total_seconds()
        self._clock = clock
        self._pause = pause
        self._owner = uuid4().hex
        self._created: list[_ExperimentRun] = []

    def stress_cpu(self, model: JujuModelHandle, unit: str, workers: int, duration: timedelta) -> None:
        if workers <= 0:
            raise ValueError("CPU workers must be positive.")
        self._create(model, unit, "pod-cpu-hog", {"CPU_CORES": str(workers)}, duration)

    def stress_memory(self, model: JujuModelHandle, unit: str, workers: int, size_mb: int, duration: timedelta) -> None:
        if workers <= 0 or size_mb <= 0:
            raise ValueError("Memory workers and size must be positive.")
        self._create(
            model,
            unit,
            "pod-memory-hog",
            {"NUMBER_OF_WORKERS": str(workers), "MEMORY_CONSUMPTION": str(size_mb)},
            duration,
        )

    def fill_disk(self, model: JujuModelHandle, unit: str, path: str, size_mb: int) -> None:
        raise NotImplementedError

    def io_latency(
        self,
        model: JujuModelHandle,
        unit: str,
        volume_path: str,
        delay: timedelta,
        percent: int,
        duration: timedelta,
    ) -> None:
        raise NotImplementedError

    def isolate_network(self, model: str, unit: str) -> None:
        raise NotImplementedError

    def remove_network_isolation(self, model: str, unit: str) -> None:
        raise NotImplementedError

    def cleanup(self, model: JujuModelHandle, unit: str, path: str) -> None:
        """Stop and remove stress experiments for this model and unit."""
        if path:
            return
        errors: list[Exception] = []
        for engine in reversed(tuple(self._created)):
            if engine.scope != (model.uri, unit):
                continue
            try:
                deadline = self._clock() + self._cleanup_timeout
                self._cleanup_engine(engine, deadline)
                self._wait(engine.setup.cleanup, deadline, engine.name)
            except Exception as error:
                errors.append(error)
            else:
                self._created.remove(engine)
                if engine.execution_error is not None:
                    errors.append(engine.execution_error)
        if errors:
            raise ChaosCleanupError(errors) from errors[0]

    def _target(self, namespace: str, unit: str) -> tuple[str, str]:
        application = unit.split("/")[0]
        pods = self._backend.core_v1_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app.kubernetes.io/name={application}",
            _request_timeout=REQUEST_TIMEOUT,
        )
        matches = [
            pod
            for pod in pods.items
            if (pod.metadata.annotations or {}).get("unit.juju.is/id") == unit
            and pod.metadata.deletion_timestamp is None
            and (pod.status is None or pod.status.phase not in {"Succeeded", "Failed"})
        ]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one live Pod for {namespace}/{unit}, found {len(matches)}.")
        pod = matches[0]
        containers = [
            container.name
            for container in pod.spec.containers
            if any(env.name == "JUJU_CONTAINER_NAME" and env.value == container.name for env in container.env or [])
        ]
        if self._target_container is not None:
            containers = [name for name in containers if name == self._target_container]
        if len(containers) != 1:
            raise RuntimeError(f"Specify one workload container for {namespace}/{unit}; candidates: {containers}.")
        return str(pod.metadata.name), containers[0]

    def _create(
        self,
        model: JujuModelHandle,
        unit: str,
        experiment: LitmusExperiment,
        settings: dict[str, str],
        duration: timedelta,
    ) -> None:
        seconds = duration.total_seconds()
        if seconds < 1 or not seconds.is_integer():
            raise ValueError("Chaos duration must be a positive whole number of seconds.")
        pod, container = self._target(model.model, unit)
        settings = {
            **settings,
            "TOTAL_CHAOS_DURATION": str(int(seconds)),
            "TARGET_PODS": pod,
            "TARGET_CONTAINER": container,
        }
        # Litmus combines Engine and experiment names in generated resource names.
        name = f"cit-{uuid4().hex[:16]}"
        setup = LitmusSetup(self._backend, model.model, name, self._owner)
        engine = _ExperimentRun((model.uri, unit), model.model, name, setup, pod)
        body = {
            "apiVersion": f"{_GROUP}/{_VERSION}",
            "kind": "ChaosEngine",
            "metadata": {
                "name": engine.name,
                "namespace": engine.namespace,
                "annotations": {OWNER_ANNOTATION: self._owner},
            },
            "spec": {
                "engineState": "active",
                "components": {"runner": {"image": RUNNER_IMAGE}},
                "terminationGracePeriodSeconds": TERMINATION_GRACE_SECONDS,
                "annotationCheck": "false",
                "chaosServiceAccount": name,
                "jobCleanUpPolicy": "delete",
                "selectors": {"pods": [{"namespace": engine.namespace, "names": pod}]},
                "experiments": [
                    {
                        "name": name,
                        "spec": {"components": {"env": [{"name": k, "value": v} for k, v in settings.items()]}},
                    }
                ],
            },
        }
        # Preparation can fail after creating some resources, before an Engine exists.
        self._created.append(engine)
        setup.prepare(experiment)
        engine.engine_requested = True
        try:
            created = self._backend.custom_objects_api.create_namespaced_custom_object(
                group=_GROUP,
                version=_VERSION,
                namespace=engine.namespace,
                plural="chaosengines",
                body=body,
                _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as error:
            if error.status == 409:
                engine.engine_requested = False
            raise
        engine.uid = created["metadata"]["uid"]
        self._wait(lambda: self._started(engine), self._clock() + self._startup_timeout, engine.name, "startup")

    @staticmethod
    def _target_status(result: dict[str, Any], pod: str) -> str | None:
        # Helpers annotate live transitions; the experiment later moves them into history.
        annotation = (result.get("metadata", {}).get("annotations") or {}).get(f"pod/{pod}")
        if annotation is not None:
            return str(annotation)
        for target in (result.get("status", {}).get("history") or {}).get("targets", []):
            if target.get("kind") == "pod" and target.get("name") == pod:
                return str(target.get("chaosStatus"))
        return None

    def _observe(self, engine: _ExperimentRun) -> list[dict[str, Any]]:
        results = self._results(engine)
        for result in results:
            status = result.get("status", {}).get("experimentStatus", {})
            if (
                status.get("verdict") in {"Fail", "Error"}
                or status.get("phase") == "Error"
                or status.get("errorOutput")
            ):
                engine.execution_error = RuntimeError(f"Litmus experiment {engine.name} failed: {status}")
            if self._target_status(result, engine.pod) in {"injected", "reverted"}:
                engine.injected = True
        return results

    def _started(self, engine: _ExperimentRun) -> bool:
        current = self._read_engine(engine)
        results = self._observe(engine)
        if engine.execution_error is not None:
            raise engine.execution_error
        if current is None:
            raise RuntimeError(f"Litmus Engine {engine.name} disappeared before stress started.")
        if current.get("status", {}).get("engineStatus") in {"completed", "stopped"} or any(
            result.get("status", {}).get("experimentStatus", {}).get("phase") in {"Completed", "Stopped"}
            or self._target_status(result, engine.pod) == "reverted"
            for result in results
        ):
            raise RuntimeError(f"Litmus experiment {engine.name} ended before stress could be observed.")
        return any(self._target_status(result, engine.pod) == "injected" for result in results)

    def _reverted(self, engine: _ExperimentRun) -> bool:
        if engine.reverted:
            return True
        results = self._results(engine)
        # A startup timeout may race with injection, so inspect results even if startup failed.
        statuses = [self._target_status(result, engine.pod) for result in results]
        if "injected" in statuses:
            engine.injected = True
        engine.reverted = bool(statuses) and all(status == "reverted" for status in statuses)
        return not engine.injected or engine.reverted

    def _read_engine(self, engine: _ExperimentRun) -> dict[str, Any] | None:
        try:
            current: dict[str, Any] = self._backend.custom_objects_api.get_namespaced_custom_object(
                group=_GROUP,
                version=_VERSION,
                namespace=engine.namespace,
                plural="chaosengines",
                name=engine.name,
                _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise
        metadata = current["metadata"]
        uid = metadata.get("uid")
        if not uid or (metadata.get("annotations") or {}).get(OWNER_ANNOTATION) != self._owner:
            raise RuntimeError(f"Cannot verify ownership of ChaosEngine {engine.namespace}/{engine.name}.")
        if engine.uid is None:
            raise RuntimeError(
                f"Creation UID was not recorded for ChaosEngine {engine.namespace}/{engine.name}; cleanup retained."
            )
        if engine.uid != uid:
            raise RuntimeError(f"ChaosEngine {engine.namespace}/{engine.name} was replaced.")
        return current

    def _cleanup_engine(self, engine: _ExperimentRun, deadline: float) -> None:
        if not engine.engine_requested:
            return
        current = self._read_engine(engine)
        observation_error: Exception | None = None
        if engine.uid is not None and not engine.stop_requested:
            try:
                self._observe(engine)
            except Exception as error:
                # A result API failure must not leave stress running. Preserve evidence for retry.
                observation_error = error
        if current is not None:
            self._backend.custom_objects_api.patch_namespaced_custom_object(
                group=_GROUP,
                version=_VERSION,
                namespace=engine.namespace,
                plural="chaosengines",
                name=engine.name,
                body={
                    "metadata": {"uid": engine.uid, "resourceVersion": current["metadata"]["resourceVersion"]},
                    "spec": {"engineState": "stop"},
                },
                _request_timeout=REQUEST_TIMEOUT,
            )
            engine.stop_requested = True
            self._wait(lambda: self._stopped(engine), deadline, engine.name)
        if engine.uid is None:
            return
        self._remove_children(engine)
        self._wait(lambda: self._children_removed(engine), deadline, engine.name)
        if observation_error is not None:
            raise observation_error
        self._wait(lambda: self._reverted(engine), deadline, engine.name, "stress reversion")
        self._remove_results(engine)
        self._wait(lambda: not self._results(engine), deadline, engine.name)
        if self._read_engine(engine) is not None:
            self._delete_custom("chaosengines", engine.namespace, engine.name, engine.uid)
            self._wait(lambda: self._read_engine(engine) is None, deadline, engine.name)

    def _stopped(self, engine: _ExperimentRun) -> bool:
        current = self._read_engine(engine)
        return current is None or current.get("status", {}).get("engineStatus") in {"stopped", "completed"}

    def _remove_children(self, engine: _ExperimentRun) -> None:
        selector = f"chaosUID={engine.uid}"
        jobs = self._batch.list_namespaced_job(
            namespace=engine.namespace, label_selector=selector, _request_timeout=REQUEST_TIMEOUT
        )
        for job in jobs.items:
            self._remember_cleanup_uid(engine, "jobs", job.metadata.name, job.metadata.uid)
            self._delete_child(self._batch.delete_namespaced_job, engine.namespace, job.metadata)
        pods = self._backend.core_v1_api.list_namespaced_pod(
            namespace=engine.namespace, label_selector=selector, _request_timeout=REQUEST_TIMEOUT
        )
        for pod in pods.items:
            self._remember_cleanup_uid(engine, "pods", pod.metadata.name, pod.metadata.uid)
            self._delete_child(self._backend.core_v1_api.delete_namespaced_pod, engine.namespace, pod.metadata)

    @staticmethod
    def _remember_cleanup_uid(engine: _ExperimentRun, kind: str, name: str, uid: str | None) -> None:
        if not uid:
            raise RuntimeError(f"Cannot verify UID of {kind} {engine.namespace}/{name}.")
        key = (kind, name)
        original = engine.cleanup_uids.setdefault(key, uid)
        if original != uid:
            raise RuntimeError(f"{kind} {engine.namespace}/{name} was replaced; cleanup retained.")

    @staticmethod
    def _delete_child(delete: Callable[..., object], namespace: str, metadata: Any) -> None:
        if not metadata.uid:
            raise RuntimeError(f"Cannot safely delete {namespace}/{metadata.name} without its UID.")
        try:
            delete(
                namespace=namespace,
                name=metadata.name,
                body=client.V1DeleteOptions(
                    preconditions=client.V1Preconditions(uid=metadata.uid), propagation_policy="Foreground"
                ),
                _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as error:
            if error.status != 404:
                raise

    def _children_removed(self, engine: _ExperimentRun) -> bool:
        selector = f"chaosUID={engine.uid}"
        jobs = self._batch.list_namespaced_job(
            namespace=engine.namespace, label_selector=selector, _request_timeout=REQUEST_TIMEOUT
        )
        pods = self._backend.core_v1_api.list_namespaced_pod(
            namespace=engine.namespace, label_selector=selector, _request_timeout=REQUEST_TIMEOUT
        )
        return not jobs.items and not pods.items

    def _remove_results(self, engine: _ExperimentRun) -> None:
        for result in self._results(engine):
            metadata = result["metadata"]
            self._remember_cleanup_uid(engine, "chaosresults", metadata["name"], metadata.get("uid"))
            self._delete_custom("chaosresults", engine.namespace, metadata["name"], metadata["uid"])

    def _results(self, engine: _ExperimentRun) -> list[dict[str, Any]]:
        response = self._backend.custom_objects_api.list_namespaced_custom_object(
            group=_GROUP,
            version=_VERSION,
            namespace=engine.namespace,
            plural="chaosresults",
            label_selector=f"chaosUID={engine.uid}",
            _request_timeout=REQUEST_TIMEOUT,
        )
        results: list[dict[str, Any]] = response["items"]
        return results

    def _delete_custom(self, plural: str, namespace: str, name: str, uid: str) -> None:
        if not uid:
            raise RuntimeError(f"Cannot safely delete {namespace}/{name} without its UID.")
        try:
            self._backend.custom_objects_api.delete_namespaced_custom_object(
                group=_GROUP,
                version=_VERSION,
                namespace=namespace,
                plural=plural,
                name=name,
                body={"preconditions": {"uid": uid}, "propagationPolicy": "Foreground"},
                _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as error:
            if error.status != 404:
                raise

    def _wait(self, check: Callable[[], bool], deadline: float, name: str, stage: str = "cleanup") -> None:
        while self._clock() < deadline:
            if check():
                return
            self._pause(min(self._poll_interval, max(0, deadline - self._clock())))
        raise TimeoutError(f"Litmus {stage} timed out for {name}.")
