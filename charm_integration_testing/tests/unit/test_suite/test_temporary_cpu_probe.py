# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""TEMPORARY SQT-905: remove with the diagnostic observer."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_suite import temporary_cpu_probe as module


@pytest.fixture
def probe() -> module.CpuStressProbe:
    return module.CpuStressProbe(
        Path("/target-config"), "controller:model", "model", "target-0", "target/0", "postgresql"
    )


def test_sql_success_is_a_read_only_bounded_local_query(
    monkeypatch: pytest.MonkeyPatch, probe: module.CpuStressProbe
) -> None:
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "Timing is on.\n1\nTime: 2.345 ms\n", ""))
    monkeypatch.setattr("test_suite.temporary_cpu_probe.subprocess.run", run)
    result = probe._sql()
    assert result["query_ok"] is True
    assert result["sql_timing"] == ["Time: 2.345 ms"]
    args = run.call_args.args[0]
    assert args[args.index("--kubeconfig") + 1] == "/target-config"
    assert "statement_timeout=3000" in " ".join(args)
    assert run.call_args.kwargs["input"] == "\\timing on\nSELECT 1;\n"
    assert run.call_args.kwargs["timeout"] == 8


def test_probe_failure_does_not_skip_cpu_or_status_or_leak_output(
    monkeypatch: pytest.MonkeyPatch, probe: module.CpuStressProbe, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(probe, "_sql", MagicMock(side_effect=subprocess.TimeoutExpired("secret command", 8)))
    metrics = MagicMock(return_value={"timestamp": "now", "window": "15s", "containers": []})
    status = MagicMock(return_value={"workload": {"current": "active"}, "agent": {"current": "idle"}})
    monkeypatch.setattr(probe, "_metrics", metrics)
    monkeypatch.setattr(probe, "_status", status)
    kernel = MagicMock(return_value={"available": False})
    monkeypatch.setattr(probe, "_kernel", kernel)
    probe.sample("during stress attempt")
    metrics.assert_called_once()
    status.assert_called_once()
    kernel.assert_called_once()
    assert "TimeoutExpired" in caplog.text
    assert "secret command" not in caplog.text


def test_missing_config_never_uses_default_cluster(
    monkeypatch: pytest.MonkeyPatch, probe: module.CpuStressProbe
) -> None:
    probe.kubeconfig = None
    run = MagicMock()
    monkeypatch.setattr(probe, "_run", run)
    with pytest.raises(ValueError):
        probe._metrics()
    run.assert_not_called()


def test_status_selects_target_unit(monkeypatch: pytest.MonkeyPatch, probe: module.CpuStressProbe) -> None:
    payload = {
        "applications": {
            "target": {
                "units": {"target/0": {"workload-status": {"current": "active"}, "juju-status": {"current": "idle"}}}
            }
        }
    }
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), ""))
    monkeypatch.setattr(probe, "_run", run)
    assert probe._status()["workload"] == {"current": "active"}
    assert "controller:model" in run.call_args.args[0]


def test_worker_stops_on_test_failure(monkeypatch: pytest.MonkeyPatch, probe: module.CpuStressProbe) -> None:
    worker = MagicMock()
    worker.is_alive.return_value = False
    monkeypatch.setattr(module, "Thread", MagicMock(return_value=worker))
    with pytest.raises(TimeoutError):
        with probe.during_stress():
            raise TimeoutError("original failure")
    assert probe.stop.is_set()
    worker.join.assert_called_once_with(timeout=40)


def test_after_cleanup_observations_do_not_claim_cleanup_succeeded(
    monkeypatch: pytest.MonkeyPatch, probe: module.CpuStressProbe
) -> None:
    sample = MagicMock()
    monkeypatch.setattr(probe, "sample", sample)
    monkeypatch.setattr("test_suite.temporary_cpu_probe.time.sleep", MagicMock())
    probe.after_cleanup(False)
    assert sample.call_count == 2
    assert all("stress may remain" in c.args[0] for c in sample.call_args_list)
    sample.side_effect = RuntimeError("diagnostic error")
    probe.after_cleanup(True)  # A diagnostic failure must not replace the original test exception.


def test_periodic_observer_samples_until_stopped(monkeypatch: pytest.MonkeyPatch, probe: module.CpuStressProbe) -> None:
    stop = MagicMock()
    stop.wait.side_effect = [False, False, True]
    monkeypatch.setattr(probe, "stop", stop)
    sample = MagicMock()
    monkeypatch.setattr(probe, "sample", sample)
    factory = MagicMock()
    factory.return_value.is_alive.return_value = False
    monkeypatch.setattr(module, "Thread", factory)
    with probe.during_stress():
        factory.call_args.kwargs["target"]()
    assert sample.call_count == 2
    stop.set.assert_called_once()


def test_sql_nonzero_exit_is_not_success(monkeypatch: pytest.MonkeyPatch, probe: module.CpuStressProbe) -> None:
    monkeypatch.setattr(probe, "_kubectl", MagicMock(return_value=subprocess.CompletedProcess([], 1, "1\n", "")))
    assert probe._sql()["query_ok"] is False


def test_kernel_counters_use_elapsed_time_and_reset_on_replacement(
    monkeypatch: pytest.MonkeyPatch, probe: module.CpuStressProbe
) -> None:
    def payload(identity: str, timestamp: int, usage: int, periods: int, throttled: int) -> str:
        return json.dumps(
            {
                "available": True,
                "identity": [identity],
                "monotonic_seconds": timestamp,
                "cpu_stat": {"usage_usec": usage, "nr_periods": periods, "nr_throttled": throttled},
                "cpu_pressure": {"some": {"total": timestamp * 100}},
            }
        )

    run = MagicMock(
        side_effect=[
            subprocess.CompletedProcess([], 0, payload("original", 10, 1_000_000, 100, 10), ""),
            subprocess.CompletedProcess([], 0, payload("original", 20, 9_000_000, 200, 90), ""),
            subprocess.CompletedProcess([], 0, payload("replacement", 30, 12_000_000, 300, 100), ""),
            subprocess.CompletedProcess([], 1, "secret", "secret"),
        ]
    )
    monkeypatch.setattr(probe, "_kubectl", run)
    assert "cpu_stat_delta" not in probe._kernel()
    second = probe._kernel()
    assert second["average_used_cores"] == 0.8
    assert second["throttled_period_fraction"] == 0.8
    assert second["pressure_total_usec_delta"] == {"some": 1000}
    assert "cpu_stat_delta" not in probe._kernel()
    with pytest.raises(RuntimeError, match="unavailable"):
        probe._kernel()
    assert probe.previous_kernel is None
    assert run.call_args.args[0][-4:] == ["timeout", "6s", "python3", "-"]


@pytest.mark.parametrize("elapsed,usage", [(0, 10), (10, -1)])
def test_kernel_does_not_report_invalid_counter_deltas(
    monkeypatch: pytest.MonkeyPatch, probe: module.CpuStressProbe, elapsed: int, usage: int
) -> None:
    previous = {"available": True, "identity": [1], "monotonic_seconds": 10, "cpu_stat": {"usage_usec": 20}}
    probe.previous_kernel = previous
    current = {**previous, "monotonic_seconds": 10 + elapsed, "cpu_stat": {"usage_usec": 20 + usage}}
    monkeypatch.setattr(
        probe, "_kubectl", MagicMock(return_value=subprocess.CompletedProcess([], 0, json.dumps(current)))
    )
    assert "average_used_cores" not in probe._kernel()
