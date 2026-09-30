# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta
from typing import Any

import pytest
from chaos_client import ChaosCleanupError
from kubernetes.client import ApiException  # type: ignore[import-untyped]

from .test_litmus_client import MODEL, UNIT, ClientContext, target_pod


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> ClientContext:
    return ClientContext(monkeypatch)


def test_memory_diagnostics_preserve_reversion_failure(
    context: "ClientContext", caplog: pytest.LogCaptureFixture
) -> None:
    chaos = context.chaos_client()
    chaos.stress_memory(MODEL, UNIT, 1, 2048, timedelta(seconds=10))
    context.auto_revert = False
    context.backend.core_v1_api.read_namespaced_pod.side_effect = ApiException(status=503)
    with caplog.at_level(logging.INFO), pytest.raises(ChaosCleanupError) as error:
        chaos.cleanup(MODEL, UNIT, "")
    assert "stress reversion" in str(error.value.errors[0])
    assert "[before cleanup]" in caplog.text
    assert "[before child deletion]" in caplog.text
    assert "[cleanup failed]" in caplog.text
    assert "target Pod unavailable" in caplog.text
    assert context.results
    context.setups[0].cleanup.assert_not_called()


def test_memory_diagnostics_capture_logs_before_deletion(
    context: "ClientContext", caplog: pytest.LogCaptureFixture
) -> None:
    chaos = context.chaos_client()
    chaos.stress_memory(MODEL, UNIT, 1, 2048, timedelta(seconds=10))
    context.children = [target_pod(name="helper")]
    context.children[0].metadata.uid = "helper-uid"
    context.children[0].metadata.labels = {
        "chaosUID": context.created[0]["metadata"].get("uid", "uid-" + context.created[0]["metadata"]["name"])
    }
    context.backend.core_v1_api.delete_namespaced_pod.side_effect = lambda **kwargs: context.children.clear()
    context.backend.core_v1_api.read_namespaced_pod.return_value = target_pod()

    def read_log(**kwargs: Any) -> str:
        assert context.children
        return "helper diagnostic output"

    context.backend.core_v1_api.read_namespaced_pod_log.side_effect = read_log
    with caplog.at_level(logging.INFO):
        chaos.cleanup(MODEL, UNIT, "")
    assert "helper diagnostic output" in caplog.text
    assert not context.children
    context.setups[0].cleanup.assert_called_once()


def test_cpu_cleanup_does_not_collect_memory_diagnostics(context: "ClientContext") -> None:
    chaos = context.chaos_client()
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=10))
    chaos.cleanup(MODEL, UNIT, "")
    context.backend.core_v1_api.read_namespaced_pod.assert_not_called()
    context.backend.core_v1_api.read_namespaced_pod_log.assert_not_called()


def test_error_source_pod_is_read_when_selector_is_empty(
    context: ClientContext, caplog: pytest.LogCaptureFixture
) -> None:
    chaos = context.chaos_client()
    chaos.stress_memory(MODEL, UNIT, 1, 2048, timedelta(seconds=30))
    context.results[0]["status"]["experimentStatus"] = {
        "phase": "Error",
        "verdict": "Error",
        "errorOutput": {"reason": '{"source":"missing-label-helper","reason":"exit status 1"}'},
    }
    helper = target_pod(name="missing-label-helper")
    context.backend.core_v1_api.read_namespaced_pod.return_value = helper
    context.backend.core_v1_api.read_namespaced_pod_log.return_value = "worker failed"
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="exit status 1"):
        chaos.check_stress(MODEL, UNIT)
    assert "experiment failed during observation" in caplog.text
    assert "worker failed" in caplog.text
    assert any(
        call.kwargs["name"] == "missing-label-helper"
        for call in context.backend.core_v1_api.read_namespaced_pod.call_args_list
    )


def test_retain_is_memory_only_and_cleanup_still_deletes(context: ClientContext) -> None:
    chaos = context.chaos_client()
    chaos.stress_memory(MODEL, UNIT, 1, 2048, timedelta(seconds=30))
    assert context.created[0]["spec"]["jobCleanUpPolicy"] == "retain"
    helper = target_pod(name="retained-helper")
    helper.metadata.uid = "retained-uid"
    helper.metadata.labels = {"chaosUID": "uid-" + context.created[0]["metadata"]["name"]}
    context.children = [helper]
    context.backend.core_v1_api.delete_namespaced_pod.side_effect = lambda **kwargs: context.children.clear()
    chaos.cleanup(MODEL, UNIT, "")
    assert not context.children
    assert not context.engines
    assert not context.results
    chaos.stress_cpu(MODEL, UNIT, 1, timedelta(seconds=30))
    assert context.created[-1]["spec"]["jobCleanUpPolicy"] == "delete"


def test_periodic_capture_keeps_logs_status_and_events_without_chaos_uid(
    context: ClientContext, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from chaos_client import temporary_memory_diagnostics as diagnostics
    from kubernetes import client as k8s  # type: ignore[import-untyped]

    now = 0.0
    monkeypatch.setattr(diagnostics, "monotonic", lambda: now)
    chaos = context.chaos_client()
    chaos.stress_memory(MODEL, UNIT, 1, 2048, timedelta(seconds=30))
    name = context.created[0]["metadata"]["name"]
    helper = target_pod(name=name + "-helper-abc")
    helper.spec.service_account_name = name
    helper.spec.node_name = "node1"
    helper.metadata.labels = {"app": name + "-helper-run"}
    helper.status = k8s.V1PodStatus(
        container_statuses=[
            k8s.V1ContainerStatus(
                name="postgresql",
                image="test",
                image_id="test",
                ready=False,
                restart_count=1,
                state=k8s.V1ContainerState(
                    terminated=k8s.V1ContainerStateTerminated(exit_code=143, signal=15, reason="Error")
                ),
            )
        ]
    )
    context.children = [helper]
    context.backend.core_v1_api.read_namespaced_pod.return_value = target_pod()
    context.backend.core_v1_api.read_namespaced_pod_log.side_effect = (
        lambda **kw: "previous worker log" if kw["previous"] else "current worker log"
    )
    event = k8s.CoreV1Event(
        metadata=k8s.V1ObjectMeta(name="eviction"),
        reason="Evicted",
        message="Memory pressure",
        involved_object=k8s.V1ObjectReference(name=helper.metadata.name),
    )
    context.backend.core_v1_api.list_namespaced_event.return_value = k8s.CoreV1EventList(items=[event])
    # Denied node access must not prevent collecting Pod evidence or fail the test.
    context.backend.core_v1_api.read_node.side_effect = ApiException(status=403)
    now = 11
    with caplog.at_level(logging.INFO):
        chaos.check_stress(MODEL, UNIT)
    assert "[observation]" in caplog.text
    assert "previous worker log" in caplog.text
    assert "current worker log" in caplog.text
    assert '"exit_code": 143' in caplog.text
    assert '"signal": 15' in caplog.text
    assert "Memory pressure" in caplog.text
    assert "node node1 unavailable" in caplog.text
    calls = context.backend.core_v1_api.read_namespaced_pod_log.call_count
    chaos.check_stress(MODEL, UNIT)
    assert context.backend.core_v1_api.read_namespaced_pod_log.call_count == calls
