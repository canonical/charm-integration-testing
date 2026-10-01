# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from chaos_client import ResourceConstraintsClient
from juju import CharmChannel, JujuApplicationInfo, JujuModelHandle
from test_suite import test_live_memory_stress_moderate as live
from test_suite.fixtures.chaos_tools import ChaosTool

from validators.base.validator import ValidationResult

MODEL = JujuModelHandle(controller="controller", model="target")
Scenario = tuple[dict[str, Any], Mock, Mock, list[float]]

NEIGHBOR = JujuModelHandle(controller="controller", model="neighbor")


def result(**overrides: Any) -> ValidationResult:
    return ValidationResult.model_validate(
        dict(
            status="PASS",
            endpoint="postgresql",
            interface="postgresql_client",
            role="requires",
            level="simple",
            relation_id=1,
        )
        | overrides
    )


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch) -> Scenario:
    clock = [0.0]
    monkeypatch.setattr(live, "monotonic", lambda: clock[0])
    monkeypatch.setattr(live, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(live, "available_chaos_tools", lambda backend: {ChaosTool.LITMUS})
    client = Mock()
    client.backend.list_applications.return_value = {
        "target": JujuApplicationInfo(
            charm="postgresql-k8s", revision=495, channel=CharmChannel.parse("14/stable"), base="22.04"
        )
    }
    client.backend.application_units.return_value = ["target/0"]
    client.validate_model.return_value = {"neighbor/0": [result()]}
    chaos = Mock()
    arguments: dict[str, Any] = dict(
        juju_client=client,
        chaos_tool_for_model=Mock(return_value=chaos),
        target_model_ref=MODEL,
        target_application="target",
        target_endpoint="database",
        neighbor_model_ref=None,
        neighbor_application="neighbor",
        neighbor_endpoint="postgresql",
        resource_constraints_client=ResourceConstraintsClient(),
        kubernetes_client=Mock(),
        memory_pressure_check_interval=timedelta(seconds=10),
        memory_pressure_recovery_timeout=timedelta(minutes=15),
    )
    return arguments, client, chaos, clock


def test_lifecycle_and_injection_time(scenario: Scenario) -> None:
    arguments, client, chaos, clock = scenario
    chaos.stress_memory.side_effect = lambda *args, **kwargs: clock.__setitem__(0, 50)
    live.test_live_memory_stress_moderate(**arguments)
    assert clock[0] == 350
    chaos.stress_memory.assert_called_once_with(
        MODEL, "target/0", workers=1, size_mb=128, duration=timedelta(seconds=420)
    )
    assert client.validate_model.call_count == 33  # Baseline, 31 observation rounds, recovery.
    assert chaos.check_stress.call_count == 62
    assert all(not call.kwargs.get("allow_completed") for call in chaos.check_stress.call_args_list)
    chaos.cleanup_all.assert_called_once()
    assert client.multi_model_idle_for_period.call_count == 2


def test_per_charm_settings_and_neighbor_model(scenario: Scenario, tmp_path: Path) -> None:
    arguments, client, chaos, clock = scenario
    (tmp_path / "postgresql-k8s.yaml").write_text(
        "constraints:\n  - criteria:\n      - track: '14'\n        ubuntu_version: '22.04'\n"
        "    memory_moderate_pressure_workers: 2\n    memory_moderate_pressure_size_mb: 64\n"
        "    memory_moderate_pressure_duration_seconds: 20\n"
    )
    arguments.update(resource_constraints_client=ResourceConstraintsClient(tmp_path), neighbor_model_ref=NEIGHBOR)
    client.validate_model.side_effect = lambda *, model, level: {"neighbor/0": [result()]} if model == NEIGHBOR else {}
    live.test_live_memory_stress_moderate(**arguments)
    chaos.stress_memory.assert_called_once_with(
        MODEL, "target/0", workers=2, size_mb=64, duration=timedelta(seconds=140)
    )
    assert clock[0] == 20
    assert {call.kwargs["model"] for call in client.validate_model.call_args_list} == {MODEL, NEIGHBOR}


@pytest.mark.parametrize(
    "results",
    [
        {},
        {"neighbor/0": [result(status="SKIPPED")]},
        {"unrelated/0": [result()]},
        {"neighbor/0": [result(endpoint="other")]},
        {"neighbor/0": [result(level="deep")]},
    ],
)
def test_no_functional_coverage_skips(scenario: Scenario, results: dict[str, list[ValidationResult]]) -> None:
    arguments, client, chaos, _ = scenario
    client.validate_model.return_value = results
    with pytest.raises(pytest.skip.Exception, match="No passing simple"):
        live.test_live_memory_stress_moderate(**arguments)
    chaos.stress_memory.assert_not_called()


@pytest.mark.parametrize("failure", ["inject", "experiment", "health", "validation", "coverage", "cleanup", "recovery"])
def test_failures_and_cleanup(scenario: Scenario, failure: str) -> None:
    arguments, client, chaos, _ = scenario
    error = RuntimeError(failure)
    if failure == "inject":
        chaos.stress_memory.side_effect = error
    elif failure == "experiment":
        chaos.check_stress.side_effect = error
    elif failure == "health":
        client.check_application_health.side_effect = [None, None, error]
    elif failure == "validation":
        client.validate_model.side_effect = [{"neighbor/0": [result()]}, error]
    elif failure == "coverage":
        client.validate_model.side_effect = [{"neighbor/0": [result()]}, {"neighbor/0": [result(status="SKIPPED")]}]
    elif failure == "cleanup":
        chaos.cleanup_all.side_effect = error
    else:
        client.multi_model_idle_for_period.side_effect = [None, error]
    with pytest.raises(RuntimeError, match="coverage" if failure == "coverage" else failure):
        live.test_live_memory_stress_moderate(**arguments)
    chaos.cleanup_all.assert_called_once()


def test_preserves_observation_and_cleanup_errors(scenario: Scenario) -> None:
    arguments, _, chaos, _ = scenario
    chaos.check_stress.side_effect = RuntimeError("experiment failed")
    chaos.cleanup_all.side_effect = RuntimeError("cleanup failed")
    with pytest.raises(RuntimeError, match="experiment failed.*cleanup failed"):
        live.test_live_memory_stress_moderate(**arguments)


@pytest.mark.parametrize("platform", ["machine", "no-tools", "mesh-without-stress"])
def test_unsupported_skips(scenario: Scenario, monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    arguments, _, chaos, _ = scenario
    if platform == "machine":
        arguments["kubernetes_client"] = None
    else:
        monkeypatch.setattr(
            live,
            "available_chaos_tools",
            lambda backend: set() if platform == "no-tools" else {ChaosTool.CHAOS_MESH},
        )
        arguments["kubernetes_client"].backend.crd_exists.return_value = False
    with pytest.raises(pytest.skip.Exception):
        live.test_live_memory_stress_moderate(**arguments)
    chaos.stress_memory.assert_not_called()


@pytest.mark.parametrize("field", ["channel", "base"])
def test_missing_metadata_fails(scenario: Scenario, field: str) -> None:
    arguments, client, chaos, _ = scenario
    info = client.backend.list_applications.return_value["target"]
    client.backend.list_applications.return_value["target"] = JujuApplicationInfo(
        charm=info.charm,
        revision=info.revision,
        channel=None if field == "channel" else info.channel,
        base=None if field == "base" else info.base,
    )
    with pytest.raises(pytest.fail.Exception, match="channel and Ubuntu base"):
        live.test_live_memory_stress_moderate(**arguments)
    chaos.stress_memory.assert_not_called()


def test_validator_runtime_counts_toward_observation(scenario: Scenario) -> None:
    _, _, chaos, clock = scenario
    validate = Mock(side_effect=lambda: clock.__setitem__(0, clock[0] + 7))
    health = Mock()
    live.observe_memory_pressure(chaos, MODEL, "target/0", 20, 10, health, validate)
    assert clock[0] == 24
    assert validate.call_count == 2
    assert health.call_count == 4


def test_late_experiment_failure_is_not_ignored(scenario: Scenario) -> None:
    arguments, _, chaos, clock = scenario

    def check(*args: Any, **kwargs: Any) -> None:
        if clock[0] >= 20:
            raise RuntimeError("stress stopped")

    chaos.check_stress.side_effect = check
    with pytest.raises(RuntimeError, match="stress stopped"):
        live.test_live_memory_stress_moderate(**arguments)
    assert clock[0] == 20
    chaos.cleanup_all.assert_called_once()


@pytest.mark.parametrize("field", ["memory_pressure_check_interval", "memory_pressure_recovery_timeout"])
def test_invalid_timeouts_fail_before_injection(scenario: Scenario, field: str) -> None:
    arguments, _, chaos, _ = scenario
    arguments[field] = timedelta(0)
    with pytest.raises(ValueError, match="must be positive"):
        live.test_live_memory_stress_moderate(**arguments)
    chaos.stress_memory.assert_not_called()


def test_missing_target_unit_fails_before_injection(scenario: Scenario) -> None:
    arguments, client, chaos, _ = scenario
    client.backend.application_units.return_value = []
    with pytest.raises(pytest.fail.Exception, match="No target units"):
        live.test_live_memory_stress_moderate(**arguments)
    chaos.stress_memory.assert_not_called()
