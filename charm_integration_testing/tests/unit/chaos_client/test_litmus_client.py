# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from chaos_client import ChaosCleanupError, MetaChaosClient
from chaos_client import litmus_client as module
from chaos_client.litmus_client import LitmusChaosClient, LitmusNotInstalledError
from chaos_client.litmus_detection import LITMUS_CRDS
from chaos_client.litmus_setup import LitmusSetup
from juju import JujuModelHandle
from kubernetes import client  # type: ignore[import-untyped]
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

MODEL = JujuModelHandle(controller="controller", model="model")
UNIT = "postgresql/0"


class BackendStub(KubernetesBackend):
    def __init__(self) -> None:
        self.api_client = MagicMock()
        self.core_v1_api = MagicMock(spec=client.CoreV1Api)
        self.custom_objects_api = MagicMock(spec=client.CustomObjectsApi)
        self.crds = set(LITMUS_CRDS)

    def crd_exists(self, name: str) -> bool:
        return name in self.crds


def target_pod(unit: str = UNIT, name: str = "postgresql-random-pod") -> Any:
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name=name, annotations={"unit.juju.is/id": unit}),
        spec=client.V1PodSpec(
            containers=[
                client.V1Container(name="charm"),
                client.V1Container(
                    name="postgresql", env=[client.V1EnvVar(name="JUJU_CONTAINER_NAME", value="postgresql")]
                ),
            ]
        ),
    )


class ClientContext:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.backend = BackendStub()
        self.batch = MagicMock(spec=client.BatchV1Api)
        self.batch.list_namespaced_job.return_value = client.V1JobList(items=[])
        self.pods = [target_pod()]
        self.children: list[Any] = []
        self.results: list[dict[str, Any]] = []
        self.engines: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.setups: list[MagicMock] = []
        self.events: list[str] = []
        self.now = 0.0
        self.prepare_error: Exception | None = None
        self.create_error: Exception | None = None
        self.create_resource = True
        self.stop_completes = True
        self.auto_inject = True
        self.auto_revert = True
        self.delete_error: Exception | None = None
        monkeypatch.setattr(client, "BatchV1Api", lambda _: self.batch)
        monkeypatch.setattr(module, "LitmusSetup", self.make_setup)
        self.backend.core_v1_api.list_namespaced_pod.side_effect = self.list_pods
        api = self.backend.custom_objects_api
        api.create_namespaced_custom_object.side_effect = self.create
        api.get_namespaced_custom_object.side_effect = self.read
        api.patch_namespaced_custom_object.side_effect = self.stop
        api.delete_namespaced_custom_object.side_effect = self.delete
        api.list_namespaced_custom_object.side_effect = self.list_results

    def list_results(self, *, label_selector: str, **kwargs: Any) -> dict[str, Any]:
        uid = label_selector.removeprefix("chaosUID=")
        return {
            "items": deepcopy([r for r in self.results if r["metadata"].get("labels", {}).get("chaosUID", uid) == uid])
        }

    def make_setup(self, backend: KubernetesBackend, namespace: str, name: str, owner: str) -> LitmusSetup:
        setup = MagicMock(spec=LitmusSetup)
        setup.prepare.side_effect = self.prepare_error

        def cleanup() -> bool:
            self.events.append(f"setup:{name}")
            return True

        setup.cleanup.side_effect = cleanup
        self.setups.append(setup)
        return cast(LitmusSetup, setup)

    def chaos_client(self, target_container: str | None = None) -> LitmusChaosClient:
        return LitmusChaosClient(
            self.backend,
            target_container=target_container,
            startup_timeout=timedelta(seconds=3),
            cleanup_timeout=timedelta(seconds=3),
            clock=lambda: self.now,
            pause=self.pause,
        )

    def pause(self, seconds: float) -> None:
        self.now += seconds

    def list_pods(self, *, label_selector: str, **kwargs: Any) -> Any:
        return client.V1PodList(items=self.children if label_selector.startswith("chaosUID=") else self.pods)

    def create(self, *, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.created.append(deepcopy(body))
        resource = deepcopy(body)
        name = resource["metadata"]["name"]
        resource["metadata"].update(uid=f"uid-{name}", resourceVersion="1")
        resource["status"] = {"engineStatus": "initialized"}
        if self.create_resource:
            self.engines[name] = resource
        if self.create_error is not None:
            raise self.create_error
        if self.auto_inject:
            self.results.append(
                {
                    "metadata": {
                        "name": f"{name}-{name}",
                        "uid": f"result-{name}",
                        "labels": {"chaosUID": f"uid-{name}"},
                        "annotations": {f"pod/{body["spec"]["selectors"]["pods"][0]["names"]}": "injected"},
                    },
                    "status": {"experimentStatus": {"phase": "Running", "verdict": "Awaited"}},
                }
            )
        return deepcopy(resource)

    def read(self, *, name: str, **kwargs: Any) -> dict[str, Any]:
        if name not in self.engines:
            raise ApiException(status=404)
        return deepcopy(self.engines[name])

    def stop(self, *, name: str, body: dict[str, Any], **kwargs: Any) -> None:
        engine = self.engines[name]
        assert body["metadata"]["uid"] == engine["metadata"]["uid"]
        assert body["spec"]["engineState"] == "stop"
        self.events.append(f"stop:{name}")
        if self.stop_completes:
            engine["status"]["engineStatus"] = "stopped"
        if self.auto_revert:
            for result in self.results:
                if (
                    result["metadata"].get("labels", {}).get("chaosUID", engine["metadata"]["uid"])
                    == engine["metadata"]["uid"]
                ):
                    result["metadata"]["annotations"] = {
                        f"pod/{engine["spec"]["selectors"]["pods"][0]["names"]}": "reverted"
                    }

    def delete(self, *, plural: str, name: str, body: dict[str, Any], **kwargs: Any) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        if plural == "chaosengines":
            assert body["preconditions"]["uid"] == self.engines[name]["metadata"]["uid"]
            del self.engines[name]
        else:
            assert plural == "chaosresults"
            result = next(result for result in self.results if result["metadata"]["name"] == name)
            assert body["preconditions"]["uid"] == result["metadata"]["uid"]
            self.results.remove(result)
        self.events.append(f"delete:{plural}:{name}")


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> ClientContext:
    return ClientContext(monkeypatch)


@pytest.mark.parametrize("missing", LITMUS_CRDS)
def test_constructor_fails_fast_for_missing_crd(context: ClientContext, missing: str) -> None:
    # GIVEN an incomplete Litmus installation
    context.backend.crds.remove(missing)

    # WHEN constructing the client, THEN the missing CRD is named and nothing is prepared
    with pytest.raises(LitmusNotInstalledError, match=missing):
        context.chaos_client()
    assert context.setups == []


class TestStress:
    @dataclass(frozen=True)
    class Params:
        operation: str
        experiment: str
        settings: dict[str, str]

    @pytest.mark.parametrize(
        "params",
        [
            Params("stress_cpu", "pod-cpu-hog", {"CPU_CORES": "2"}),
            Params("stress_memory", "pod-memory-hog", {"NUMBER_OF_WORKERS": "2", "MEMORY_CONSUMPTION": "128"}),
        ],
        ids=lambda params: params.operation,
    )
    def test_creates_engine_for_exact_unit_and_workload(self, context: ClientContext, params: Params) -> None:
        # GIVEN a target Pod with a charm container and another unit in the same application
        context.pods.append(target_pod("postgresql/1", "another-pod"))
        chaos = context.chaos_client()
        assert context.setups == []

        # WHEN requesting stress
        if params.operation == "stress_cpu":
            chaos.stress_cpu(MODEL, UNIT, 2, timedelta(seconds=30))
        else:
            chaos.stress_memory(MODEL, UNIT, 2, 128, timedelta(seconds=30))

        # THEN the Engine uses a unique definition, the exact Pod and the workload container
        engine = context.created[0]
        name = engine["metadata"]["name"]
        spec = engine["spec"]
        assert "appinfo" not in spec
        assert spec["selectors"] == {"pods": [{"namespace": MODEL.model, "names": "postgresql-random-pod"}]}
        assert spec["chaosServiceAccount"] == name
        assert spec["components"]["runner"]["image"] == "docker.io/litmuschaos/chaos-runner:3.31.0"
        assert spec["terminationGracePeriodSeconds"] == 30
        assert spec["experiments"][0]["name"] == name
        env = {entry["name"]: entry["value"] for entry in spec["experiments"][0]["spec"]["components"]["env"]}
        assert params.settings.items() <= env.items()
        assert env["TARGET_PODS"] == "postgresql-random-pod"
        assert spec["selectors"]["pods"][0]["names"] == env["TARGET_PODS"]
        assert env["TARGET_CONTAINER"] == "postgresql"
        assert env["TOTAL_CHAOS_DURATION"] == "30"
        assert len(f"{name}-{name}") <= 63
        context.setups[0].prepare.assert_called_once_with(params.experiment)


@pytest.mark.parametrize("pods", [[], [target_pod(), target_pod(name="duplicate")]], ids=["missing", "ambiguous"])
def test_invalid_target_does_not_prepare_resources(context: ClientContext, pods: list[Any]) -> None:
    # GIVEN no unique Pod corresponding to the requested unit
    context.pods = pods

    # WHEN requesting stress, THEN target resolution fails before preparation
    with pytest.raises(RuntimeError, match="Expected one live Pod"):
        context.chaos_client().stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    assert context.setups == []
    assert context.created == []


def test_multiple_workload_containers_require_explicit_selection(context: ClientContext) -> None:
    # GIVEN two workload containers
    context.pods[0].spec.containers.append(
        client.V1Container(name="exporter", env=[client.V1EnvVar(name="JUJU_CONTAINER_NAME", value="exporter")])
    )

    # WHEN no container is selected, THEN nothing is created
    with pytest.raises(RuntimeError, match="Specify one workload container"):
        context.chaos_client().stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    assert context.created == []

    # WHEN explicitly selecting the workload, THEN stress can be requested
    context.chaos_client("postgresql").stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    assert len(context.created) == 1


@pytest.mark.parametrize("duration", [timedelta(0), timedelta(seconds=-1), timedelta(milliseconds=1500)])
def test_invalid_duration_does_not_prepare_resources(context: ClientContext, duration: timedelta) -> None:
    # GIVEN an invalid duration
    chaos = context.chaos_client()

    # WHEN requesting stress, THEN validation precedes all creation
    with pytest.raises(ValueError):
        chaos.stress_cpu(MODEL, UNIT, 1, duration)
    assert context.setups == []


def test_preparation_failure_is_cleaned_through_meta_client(context: ClientContext) -> None:
    # GIVEN a failure after setup may have created some resources
    failure = ApiException(status=403)
    context.prepare_error = failure
    meta = MetaChaosClient([context.chaos_client()])

    # WHEN executing and tearing down
    with pytest.raises(ApiException) as exc_info:
        meta.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    meta.cleanup_all()

    # THEN the original error propagates and partial setup is cleaned without reading an Engine
    assert exc_info.value is failure
    context.setups[0].cleanup.assert_called_once()
    context.backend.custom_objects_api.get_namespaced_custom_object.assert_not_called()
    assert context.created == []


@pytest.mark.parametrize("replaced", [False, True], ids=["original", "replacement"])
@pytest.mark.parametrize("created", [False, True], ids=["not-created", "response-lost"])
def test_failed_engine_post_retains_unverified_identity(context: ClientContext, created: bool, replaced: bool) -> None:
    # GIVEN a lost Engine POST response
    failure = TimeoutError("Lost response")
    context.create_error = failure
    context.create_resource = created
    chaos = context.chaos_client()
    meta = MetaChaosClient([chaos])

    # WHEN execution fails and teardown runs
    with pytest.raises(TimeoutError) as exc_info:
        meta.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    assert exc_info.value is failure
    if created:
        name = context.created[0]["metadata"]["name"]
        if replaced:
            context.engines[name]["metadata"]["uid"] = "replacement"
        for _ in range(2):
            with pytest.raises(ChaosCleanupError) as cleanup_error:
                meta.cleanup_all()
            nested = cleanup_error.value.errors[0]
            assert isinstance(nested, ChaosCleanupError)
            assert "Creation UID was not recorded" in str(nested.errors[0])
            assert len(chaos._created) == 1
            assert chaos._created[0].uid is None
            assert name in context.engines
            context.backend.custom_objects_api.patch_namespaced_custom_object.assert_not_called()
            context.backend.custom_objects_api.delete_namespaced_custom_object.assert_not_called()
            context.batch.delete_namespaced_job.assert_not_called()
            context.backend.core_v1_api.delete_namespaced_pod.assert_not_called()
            context.setups[0].cleanup.assert_not_called()
        context.engines.clear()
    meta.cleanup_all()
    assert chaos._created == []
    context.setups[0].cleanup.assert_called_once()


def test_engine_conflict_still_cleans_prepared_resources(context: ClientContext) -> None:
    # GIVEN a rejected Engine creation after successful preparation
    context.create_error = ApiException(status=409)
    context.create_resource = False
    meta = MetaChaosClient([context.chaos_client()])

    # WHEN execution fails and teardown runs
    with pytest.raises(ApiException):
        meta.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    meta.cleanup_all()

    # THEN the conflicting Engine is untouched while this execution's setup is cleaned
    context.backend.custom_objects_api.get_namespaced_custom_object.assert_not_called()
    context.backend.custom_objects_api.delete_namespaced_custom_object.assert_not_called()
    context.setups[0].cleanup.assert_called_once()


def test_cleanup_removes_result_and_engine_before_permissions(context: ClientContext) -> None:
    # GIVEN an execution with a retained result
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    name = context.created[0]["metadata"]["name"]
    context.results = [{"metadata": {"name": "result", "uid": "result-uid"}}]

    # WHEN cleaning up
    chaos.cleanup(MODEL, UNIT, "")

    # THEN stopping and execution cleanup precede removal of permissions
    assert context.events == [
        f"stop:{name}",
        "delete:chaosresults:result",
        f"delete:chaosengines:{name}",
        f"setup:{name}",
    ]
    chaos.cleanup(MODEL, UNIT, "")
    context.setups[0].cleanup.assert_called_once()


def test_stop_timeout_retains_permissions_for_retry(context: ClientContext) -> None:
    # GIVEN an operator that does not acknowledge stop
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    context.stop_completes = False

    # WHEN cleanup times out, THEN execution dependencies remain
    with pytest.raises(ChaosCleanupError) as exc_info:
        chaos.cleanup(MODEL, UNIT, "")
    assert isinstance(exc_info.value.errors[0], TimeoutError)
    context.setups[0].cleanup.assert_not_called()

    # WHEN the operator recovers, THEN cleanup can finish
    context.stop_completes = True
    chaos.cleanup(MODEL, UNIT, "")
    assert context.engines == {}
    context.setups[0].cleanup.assert_called_once()


def test_terminating_helper_prevents_dependency_cleanup(context: ClientContext) -> None:
    # GIVEN a helper Pod that remains after its deletion request
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    context.children = [client.V1Pod(metadata=client.V1ObjectMeta(name="helper", uid="helper-uid"))]

    # WHEN cleanup waits for termination, THEN a timeout retains execution dependencies
    with pytest.raises(ChaosCleanupError):
        chaos.cleanup(MODEL, UNIT, "")
    context.setups[0].cleanup.assert_not_called()
    delete = context.backend.core_v1_api.delete_namespaced_pod.call_args.kwargs
    assert delete["body"].preconditions.uid == "helper-uid"

    # WHEN the helper disappears, THEN cleanup succeeds
    context.children.clear()
    chaos.cleanup(MODEL, UNIT, "")
    context.setups[0].cleanup.assert_called_once()


def test_cleanup_respects_unit_and_path_scopes(context: ClientContext) -> None:
    # GIVEN two executions in one model
    context.pods.append(target_pod("postgresql/1", "second-pod"))
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    chaos.stress_memory(MODEL, "postgresql/1", 1, 128, timedelta(seconds=10))

    # WHEN cleaning an unrelated path and then the first unit
    chaos.cleanup(MODEL, UNIT, "/data")
    assert len(context.engines) == 2
    chaos.cleanup(MODEL, UNIT, "")

    # THEN only that unit's stress and permissions are removed
    assert len(context.engines) == 1
    context.setups[0].cleanup.assert_called_once()
    context.setups[1].cleanup.assert_not_called()


def test_replaced_engine_is_not_stopped_or_deleted(context: ClientContext) -> None:
    # GIVEN an Engine replaced after creation
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    name = context.created[0]["metadata"]["name"]
    context.engines[name]["metadata"]["uid"] = "replacement"

    # WHEN cleanup is requested, THEN replacement identity prevents modification
    with pytest.raises(ChaosCleanupError):
        chaos.cleanup(MODEL, UNIT, "")
    context.backend.custom_objects_api.patch_namespaced_custom_object.assert_not_called()
    context.backend.custom_objects_api.delete_namespaced_custom_object.assert_not_called()
    context.setups[0].cleanup.assert_not_called()


def test_engine_delete_failure_keeps_permissions_until_retry(context: ClientContext) -> None:
    # GIVEN an Engine delete that fails after the experiment has stopped
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    context.delete_error = ApiException(status=503)

    # WHEN cleanup fails, THEN permissions remain available
    with pytest.raises(ChaosCleanupError):
        chaos.cleanup(MODEL, UNIT, "")
    context.setups[0].cleanup.assert_not_called()

    # WHEN deletion succeeds again, THEN both execution and setup cleanup finish
    context.delete_error = None
    chaos.cleanup(MODEL, UNIT, "")
    assert context.engines == {}
    context.setups[0].cleanup.assert_called_once()


def test_setup_cleanup_failure_is_retryable_after_engine_deletion(context: ClientContext) -> None:
    # GIVEN setup cleanup that fails after Engine deletion
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    context.setups[0].cleanup.side_effect = ApiException(status=503)

    # WHEN cleanup fails, THEN the Engine is gone but setup remains tracked
    with pytest.raises(ChaosCleanupError):
        chaos.cleanup(MODEL, UNIT, "")
    assert context.engines == {}

    # WHEN setup cleanup recovers, THEN retry tolerates the absent Engine
    context.setups[0].cleanup.side_effect = None
    context.setups[0].cleanup.return_value = True
    chaos.cleanup(MODEL, UNIT, "")
    assert context.setups[0].cleanup.call_count == 2


def test_unsupported_operations_never_prepare_resources(context: ClientContext) -> None:
    # GIVEN a Litmus client with no executions
    chaos = context.chaos_client()

    # WHEN requesting unsupported operations, THEN each allows fallback without creating resources
    with pytest.raises(NotImplementedError):
        chaos.fill_disk(MODEL, UNIT, "/tmp/fill", 128)
    with pytest.raises(NotImplementedError):
        chaos.io_latency(MODEL, UNIT, "/data", timedelta(seconds=1), 50, timedelta(seconds=10))
    with pytest.raises(NotImplementedError):
        chaos.isolate_network(MODEL.model, UNIT)
    with pytest.raises(NotImplementedError):
        chaos.remove_network_isolation(MODEL.model, UNIT)
    assert context.setups == []
    assert context.created == []


def test_startup_waits_for_injection_not_engine_creation(context: ClientContext) -> None:
    context.auto_inject = False
    chaos = context.chaos_client()
    with pytest.raises(TimeoutError, match="startup"):
        chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    assert context.now == 3
    chaos.cleanup(MODEL, UNIT, "")
    assert not context.engines


@pytest.mark.parametrize("verdict", ["Fail", "Error"])
def test_startup_reports_failed_result(context: ClientContext, verdict: str) -> None:
    context.auto_inject = False
    context.results = [{"metadata": {}, "status": {"experimentStatus": {"phase": "Error", "verdict": verdict}}}]
    with pytest.raises(RuntimeError, match="experiment .* failed"):
        context.chaos_client().stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    assert context.now == 0


def test_late_failure_is_reported_after_cleanup(context: ClientContext) -> None:
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    context.results[0]["status"]["experimentStatus"] = {
        "phase": "Error",
        "verdict": "Error",
        "errorOutput": {"reason": "helper failed"},
    }
    with pytest.raises(ChaosCleanupError) as error:
        chaos.cleanup(MODEL, UNIT, "")
    assert "helper failed" in str(error.value.errors[0])
    assert not context.engines
    assert not context.results
    context.setups[0].cleanup.assert_called_once()
    chaos.cleanup(MODEL, UNIT, "")


def test_missing_reversion_retains_result_and_permissions(context: ClientContext) -> None:
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    context.auto_revert = False
    with pytest.raises(ChaosCleanupError) as error:
        chaos.cleanup(MODEL, UNIT, "")
    assert "reversion" in str(error.value.errors[0])
    assert context.results
    context.setups[0].cleanup.assert_not_called()
    context.auto_revert = True
    chaos.cleanup(MODEL, UNIT, "")
    assert not context.results


def test_reversion_history_survives_annotation_removal(context: ClientContext) -> None:
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    context.auto_revert = False
    result = context.results[0]
    result["metadata"].pop("annotations")
    result["status"]["history"] = {
        "targets": [{"kind": "pod", "name": context.pods[0].metadata.name, "chaosStatus": "reverted"}]
    }
    result["status"]["experimentStatus"] = {"phase": "Completed", "verdict": "Pass"}
    chaos.cleanup(MODEL, UNIT, "")
    assert not context.engines


def test_startup_api_error_propagates(context: ClientContext) -> None:
    context.backend.custom_objects_api.list_namespaced_custom_object.side_effect = ApiException(status=403)
    with pytest.raises(ApiException) as error:
        context.chaos_client().stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    assert error.value.status == 403


def test_startup_waits_through_running_until_injection(context: ClientContext) -> None:
    context.auto_inject = False

    def progress(seconds: float) -> None:
        context.now += seconds
        name = next(iter(context.engines))
        context.results = [
            {
                "metadata": {
                    "name": name,
                    "uid": "result-uid",
                    "annotations": {"pod/postgresql-random-pod": "targeted" if context.now < 2 else "injected"},
                },
                "status": {"experimentStatus": {"phase": "Running", "verdict": "Awaited"}},
            }
        ]

    context.pause = progress
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    assert context.now == 2
    chaos.cleanup(MODEL, UNIT, "")


def test_completed_run_is_not_reported_as_active_stress(context: ClientContext) -> None:
    context.auto_inject = False
    context.results = [
        {
            "metadata": {"annotations": {"pod/postgresql-random-pod": "injected"}},
            "status": {"experimentStatus": {"phase": "Completed", "verdict": "Pass"}},
        }
    ]
    with pytest.raises(RuntimeError, match="ended before stress"):
        context.chaos_client().stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))


def test_helper_error_is_reported_before_final_verdict(context: ClientContext) -> None:
    context.auto_inject = False
    context.results = [
        {
            "metadata": {},
            "status": {
                "experimentStatus": {
                    "phase": "Running",
                    "verdict": "Awaited",
                    "errorOutput": {"reason": "injection failed"},
                }
            },
        }
    ]
    with pytest.raises(RuntimeError, match="injection failed"):
        context.chaos_client().stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))


def test_result_read_failure_still_stops_stress_and_retains_evidence(context: ClientContext) -> None:
    # GIVEN active stress followed by a failure to read its result
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    api = context.backend.custom_objects_api
    api.list_namespaced_custom_object.side_effect = ApiException(status=503)

    # WHEN cleaning up, THEN stress is stopped but results and permissions are retained
    with pytest.raises(ChaosCleanupError):
        chaos.cleanup(MODEL, UNIT, "")
    api.patch_namespaced_custom_object.assert_called_once()
    assert context.results
    context.setups[0].cleanup.assert_not_called()

    # WHEN the result API recovers, THEN cleanup can finish
    api.list_namespaced_custom_object.side_effect = context.list_results
    chaos.cleanup(MODEL, UNIT, "")
    assert not context.results


def test_rejected_stop_does_not_suppress_later_execution_failure(context: ClientContext) -> None:
    # GIVEN active stress and a rejected stop request
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    api = context.backend.custom_objects_api
    api.patch_namespaced_custom_object.side_effect = ApiException(status=409)
    with pytest.raises(ChaosCleanupError):
        chaos.cleanup(MODEL, UNIT, "")

    # WHEN execution fails before the successful retry, THEN its failure is still reported
    context.results[0]["status"]["experimentStatus"] = {"phase": "Error", "verdict": "Error"}
    api.patch_namespaced_custom_object.side_effect = context.stop
    with pytest.raises(ChaosCleanupError) as error:
        chaos.cleanup(MODEL, UNIT, "")
    assert "experiment" in str(error.value.errors[0])
    assert not context.engines
    context.setups[0].cleanup.assert_called_once()
