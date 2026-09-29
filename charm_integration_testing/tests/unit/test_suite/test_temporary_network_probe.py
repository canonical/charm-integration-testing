# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""TEMPORARY SQT-909: remove with the network diagnostics."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from test_suite import temporary_network_probe as module


def pod(name: str, uid: str, unit: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            uid=uid,
            labels={"app.kubernetes.io/name": "target"} if unit else {},
            annotations={"unit.juju.is/id": unit},
            deletion_timestamp=None,
        ),
        status=SimpleNamespace(
            phase="Running", pod_ip="10.0.0.1", conditions=[SimpleNamespace(type="Ready", status="True")]
        ),
        spec=SimpleNamespace(node_name="node"),
    )


@pytest.fixture
def probe() -> module.NetworkIsolationProbe:
    backend = MagicMock()
    obj = module.NetworkIsolationProbe(backend, Path("/target-config"), "model", "target/0")
    backend.core_v1_api.list_namespaced_pod.return_value.items = [pod("target-0", "target-uid", "target/0")]
    backend.core_v1_api.create_namespaced_pod.return_value = pod(obj.name, "probe-uid")
    backend.core_v1_api.read_namespaced_pod.return_value = pod(obj.name, "probe-uid")
    backend.networking_v1_api.list_namespaced_network_policy.return_value.items = []
    return obj


def test_prepare_and_delete_use_creation_uid(probe: module.NetworkIsolationProbe) -> None:
    probe.prepare()
    assert probe.ready
    spec = probe.backend.core_v1_api.create_namespaced_pod.call_args.kwargs["body"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    probe.backend.core_v1_api.read_namespaced_pod.side_effect = ApiException(status=404)
    probe.cleanup()
    assert probe.backend.core_v1_api.delete_namespaced_pod.call_args.kwargs["body"]["preconditions"] == {
        "uid": "probe-uid"
    }


def test_lost_creation_response_cannot_adopt_replacement(probe: module.NetworkIsolationProbe) -> None:
    probe.backend.core_v1_api.create_namespaced_pod.side_effect = TimeoutError()
    probe.prepare()
    assert not probe.ready
    with pytest.raises(RuntimeError, match="UID unknown"):
        probe.cleanup()
    probe.backend.core_v1_api.delete_namespaced_pod.assert_not_called()


def test_delete_conflict_is_reported_and_never_retried_with_new_uid(probe: module.NetworkIsolationProbe) -> None:
    probe.prepare()
    api = probe.backend.core_v1_api
    api.delete_namespaced_pod.side_effect = ApiException(status=409)
    for _ in range(2):
        with pytest.raises(ApiException):
            probe.cleanup()
    assert all(
        c.kwargs["body"]["preconditions"] == {"uid": "probe-uid"} for c in api.delete_namespaced_pod.call_args_list
    )


def test_tcp_failures_are_distinct_from_exec_failures(
    monkeypatch: pytest.MonkeyPatch, probe: module.NetworkIsolationProbe
) -> None:
    probe.prepare()
    run = MagicMock(
        return_value=subprocess.CompletedProcess([], 0, 'SQT909_TCP={"connected":false,"error":"TimeoutError"}\n', "")
    )
    monkeypatch.setattr("test_suite.temporary_network_probe.subprocess.run", run)
    assert probe._connect()["connected"] is False
    assert run.call_args.kwargs["timeout"] == 10
    assert run.call_args.args[0][-1] == "10.0.0.1"
    assert "/target-config" in run.call_args.args[0]
    run.return_value = subprocess.CompletedProcess([], 1, "", "exec error")
    with pytest.raises(RuntimeError, match="not evidence"):
        probe._connect()


def test_replaced_target_is_inconclusive(monkeypatch: pytest.MonkeyPatch, probe: module.NetworkIsolationProbe) -> None:
    probe.prepare()
    probe.target = ("target-0", "original-uid", "10.0.0.1")
    run = MagicMock()
    monkeypatch.setattr("test_suite.temporary_network_probe.subprocess.run", run)
    with pytest.raises(RuntimeError, match="identity/IP changed"):
        probe._connect()
    run.assert_not_called()


def test_read_failure_does_not_prevent_connectivity_sample(
    monkeypatch: pytest.MonkeyPatch, probe: module.NetworkIsolationProbe
) -> None:
    probe.backend.networking_v1_api.list_namespaced_network_policy.side_effect = PermissionError()
    connect = MagicMock(return_value={"connected": True})
    monkeypatch.setattr(probe, "_connect", connect)
    probe.sample("before isolation")
    connect.assert_called_once()


def test_unavailable_probe_cannot_report_blocked_and_never_uses_default_config(
    probe: module.NetworkIsolationProbe, caplog: pytest.LogCaptureFixture
) -> None:
    probe.kubeconfig = None
    probe.prepare()
    probe.sample("before isolation")
    probe.cleanup()
    probe.backend.core_v1_api.create_namespaced_pod.assert_not_called()
    assert "inconclusive" in caplog.text


def test_after_removal_rechecks_and_labels_failure(
    monkeypatch: pytest.MonkeyPatch, probe: module.NetworkIsolationProbe
) -> None:
    sample = MagicMock()
    monkeypatch.setattr(probe, "sample", sample)
    monkeypatch.setattr("test_suite.temporary_network_probe.time.sleep", MagicMock())
    probe.after_removal(False)
    assert [c.args[0] for c in sample.call_args_list] == ["after removal failure", "after removal failure +5s"]
