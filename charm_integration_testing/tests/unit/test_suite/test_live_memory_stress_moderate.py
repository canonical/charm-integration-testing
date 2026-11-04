# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from logging import getLogger
from pathlib import Path
from traceback import extract_tb
from typing import Any
from unittest.mock import Mock

import pytest
from chaos_client import ChaosResourceConstraintsError, MetaChaosClient, ResourceConstraintsClient
from juju import CharmChannel, JujuApplicationInfo, JujuClient, JujuModelHandle, JujuValidationError
from kubernetes_client import KubernetesClientRegistry
from test_suite import moderate_stress
from test_suite import test_live_memory_stress_moderate as live

from validators.base.validator import ValidationResult

from ..extensions.shared import NullJujuBackend

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
    monkeypatch.setattr(moderate_stress, "monotonic", lambda: clock[0])
    monkeypatch.setattr(moderate_stress, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    client = Mock()
    client.backend.kubernetes_cloud_for_model.return_value = None
    client.backend.list_applications.return_value = {
        "target": JujuApplicationInfo(
            charm="postgresql-k8s", revision=495, channel=CharmChannel.parse("14/stable"), base="22.04"
        )
    }
    client.backend.application_units.return_value = ["target/0"]
    client.validate_model.return_value = {"target/0": [result(endpoint="database")], "neighbor/0": [result()]}
    chaos = Mock()
    chaos.supports.return_value = True
    chaos.stress_memory.return_value = timedelta(minutes=5)
    arguments: dict[str, Any] = dict(
        juju_client=client,
        chaos_tool_for_model=Mock(return_value=chaos),
        target_model_ref=MODEL,
        target_application="target",
        target_endpoint="database",
        neighbor_model_ref=None,
        neighbor_application="neighbor",
        neighbor_endpoint="postgresql",
        memory_pressure_check_interval=timedelta(seconds=10),
        memory_pressure_recovery_timeout=timedelta(minutes=15),
        memory_pressure_workers=1,
        memory_pressure_duration=timedelta(minutes=5),
        memory_pressure_size_mb=128,
        memory_pressure_duration_margin=timedelta(minutes=2),
        kubernetes_client_registry=KubernetesClientRegistry({}),
    )
    return arguments, client, chaos, clock


@pytest.mark.parametrize("startup", [50, 130], ids=["fast-startup", "startup-exceeds-margin"])
def test_lifecycle_and_injection_time(scenario: Scenario, startup: float) -> None:
    arguments, client, chaos, clock = scenario
    chaos.stress_memory.side_effect = lambda *args, **kwargs: (clock.__setitem__(0, startup), timedelta(minutes=5))[1]
    live.test_live_memory_stress_moderate(**arguments)
    assert clock[0] == startup + 300
    chaos.stress_memory.assert_called_once_with(
        MODEL,
        "target/0",
        workers=1,
        size_mb=128,
        duration=timedelta(seconds=300),
        scenario="moderate_pressure",
        duration_margin=timedelta(minutes=2),
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
    meta = MetaChaosClient([chaos], client.backend, ResourceConstraintsClient(tmp_path))
    arguments.update(chaos_tool_for_model=Mock(return_value=meta), neighbor_model_ref=NEIGHBOR)
    client.validate_model.side_effect = lambda *, model, level, applications, endpoints: (
        {"neighbor/0": [result()]} if model == NEIGHBOR else {"target/0": [result(endpoint="database")]}
    )
    live.test_live_memory_stress_moderate(**arguments)
    chaos.stress_memory.assert_called_once_with(MODEL, "target/0", 2, 64, timedelta(seconds=140))
    client.backend.list_applications.assert_called_once_with(MODEL)
    assert clock[0] == 20
    assert {call.kwargs["model"] for call in client.validate_model.call_args_list} == {MODEL, NEIGHBOR}


@pytest.mark.parametrize("neighbor_model", [MODEL, NEIGHBOR], ids=["same-model", "cross-model"])
@pytest.mark.parametrize("failing_application", ["unrelated", "target", "neighbor"])
@pytest.mark.parametrize("selected_endpoint", [False, True])
def test_validation_scopes_applications_before_running(
    neighbor_model: JujuModelHandle, failing_application: str, selected_endpoint: bool
) -> None:
    # GIVEN a real validation client and a failing application in the model
    calls: list[tuple[JujuModelHandle, str]] = []

    class Backend(NullJujuBackend):
        def list_applications(self, model: JujuModelHandle) -> dict[str, JujuApplicationInfo]:
            return {
                app: JujuApplicationInfo(
                    charm="postgresql-k8s", revision=495, channel=CharmChannel.parse("14/stable"), base="22.04"
                )
                for app in ("target", "neighbor", "unrelated")
            }

        def validate_application(
            self, model: JujuModelHandle, application: str, level: str
        ) -> dict[str, list[ValidationResult]]:
            calls.append((model, application))
            return {
                f"{application}/0": [
                    result(endpoint="database" if application == "target" else "postgresql"),
                    result(
                        endpoint=("database" if application == "target" else "postgresql")
                        if selected_endpoint
                        else ("postgresql" if application == "target" else "database"),
                        status="FAIL" if application == failing_application else "PASS",
                    ),
                ]
            }

    client = JujuClient(Backend(), getLogger(__name__))
    endpoints = [(MODEL, "target", "database"), (neighbor_model, "neighbor", "postgresql")]
    # WHEN validating duplicate endpoints, THEN each selected application runs once
    if failing_application == "unrelated" or not selected_endpoint:
        passed = moderate_stress.validate_service(client, endpoints + [endpoints[0]])
        assert passed == {
            (MODEL, "target/0", "database", "postgresql_client", 1),
            (neighbor_model, "neighbor/0", "postgresql", "postgresql_client", 1),
        }
        assert calls == [(MODEL, "target"), (neighbor_model, "neighbor")]
    else:
        # A selected application's failure must still propagate.
        with pytest.raises(JujuValidationError):
            moderate_stress.validate_service(client, endpoints)


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


@pytest.mark.parametrize(
    ("failure", "cleanup_fails"),
    [(phase, False) for phase in ("inject", "experiment", "health", "validation", "coverage", "cleanup", "recovery")]
    + [("inject", True), ("experiment", True)],
)
def test_failures_and_cleanup(scenario: Scenario, failure: str, cleanup_fails: bool) -> None:
    arguments, client, chaos, _ = scenario
    error = RuntimeError(failure)
    if failure == "inject":
        chaos.stress_memory.side_effect = error
    elif failure == "experiment":
        chaos.check_stress.side_effect = error
    elif failure == "health":
        client.check_application_health.side_effect = [None, None, error]
    elif failure == "validation":
        client.validate_model.side_effect = [client.validate_model.return_value, error]
    elif failure == "coverage":
        client.validate_model.side_effect = [
            client.validate_model.return_value,
            {"neighbor/0": [result(status="SKIPPED")]},
        ]
    elif failure == "cleanup":
        chaos.cleanup_all.side_effect = error
    else:
        client.multi_model_idle_for_period.side_effect = [None, error]
    cleanup_error = RuntimeError("cleanup failed")
    if cleanup_fails:
        chaos.cleanup_all.side_effect = cleanup_error
    with pytest.raises(RuntimeError, match="coverage" if failure == "coverage" else failure) as caught:
        live.test_live_memory_stress_moderate(**arguments)
    chaos.cleanup_all.assert_called_once()
    if cleanup_fails:
        assert caught.value is error
        assert caught.value.__cause__ is cleanup_error
        assert cleanup_error.__traceback__ is not None
    elif failure != "coverage":
        assert caught.value is error


def fail_observation(*args: Any, **kwargs: Any) -> None:
    raise TimeoutError("observation timed out")


def test_cleanup_preserves_observation_type_and_traceback(scenario: Scenario) -> None:
    arguments, _, chaos, _ = scenario
    chaos.check_stress.side_effect = fail_observation
    cleanup_error = ValueError("cleanup failed")
    chaos.cleanup_all.side_effect = cleanup_error

    with pytest.raises(TimeoutError, match="observation timed out") as caught:
        live.test_live_memory_stress_moderate(**arguments)

    assert caught.value.__cause__ is cleanup_error
    assert any(frame.name == "fail_observation" for frame in extract_tb(caught.value.__traceback__))
    chaos.cleanup_all.assert_called_once()


def test_unsupported_skips(scenario: Scenario) -> None:
    arguments, client, chaos, _ = scenario
    chaos.supports.return_value = False
    with pytest.raises(pytest.skip.Exception, match="No available chaos client"):
        live.test_live_memory_stress_moderate(**arguments)
    chaos.supports.assert_called_once_with("stress_memory")
    chaos.stress_memory.assert_not_called()
    client.multi_model_idle_for_period.assert_not_called()


def test_chaos_detection_error_is_not_skipped(scenario: Scenario) -> None:
    arguments, _, chaos, _ = scenario
    arguments["chaos_tool_for_model"].side_effect = RuntimeError("API unavailable")
    with pytest.raises(RuntimeError, match="API unavailable"):
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
    meta = MetaChaosClient([chaos], client.backend, ResourceConstraintsClient())
    arguments["chaos_tool_for_model"] = Mock(return_value=meta)
    with pytest.raises(ChaosResourceConstraintsError, match="Incomplete application metadata"):
        live.test_live_memory_stress_moderate(**arguments)
    chaos.stress_memory.assert_not_called()


def test_validator_runtime_counts_toward_observation(scenario: Scenario) -> None:
    _, _, chaos, clock = scenario
    validate = Mock(side_effect=lambda: clock.__setitem__(0, clock[0] + 7))
    health = Mock()
    moderate_stress.observe_moderate_pressure(chaos, MODEL, "target/0", 20, 10, health, validate, stress_deadline=140)
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


@pytest.mark.parametrize("startup", [0, 130], ids=["immediate-startup", "startup-exceeds-margin"])
def test_execution_budget_overrun_fails_and_cleans_up(scenario: Scenario, startup: float) -> None:
    arguments, client, chaos, clock = scenario
    chaos.stress_memory.side_effect = lambda *args, **kwargs: (clock.__setitem__(0, startup), timedelta(minutes=5))[1]
    calls = 0

    def validate(**kwargs: Any) -> dict[str, list[ValidationResult]]:
        nonlocal calls
        calls += 1
        if calls > 1:
            clock[0] += 421
        return {"target/0": [result(endpoint="database")], "neighbor/0": [result()]}

    client.validate_model.side_effect = validate
    with pytest.raises(TimeoutError, match="execution budget"):
        live.test_live_memory_stress_moderate(**arguments)
    chaos.cleanup_all.assert_called_once()
    assert client.multi_model_idle_for_period.call_count == 1
    assert clock[0] == startup + 421


@pytest.mark.parametrize("neighbor_model", [None, NEIGHBOR], ids=["same-model", "cross-model"])
@pytest.mark.parametrize("missing_application", ["target", "neighbor"])
@pytest.mark.parametrize("coverage", ["absent", "skipped", "wrong-endpoint"])
def test_partial_baseline_coverage_skips_before_injection(
    scenario: Scenario, neighbor_model: JujuModelHandle | None, missing_application: str, coverage: str
) -> None:
    # GIVEN one selected endpoint has no passing simple validator
    arguments, client, chaos, _ = scenario
    arguments["neighbor_model_ref"] = neighbor_model

    def validate(
        *, model: JujuModelHandle, level: str, applications: list[str], endpoints: set[tuple[str, str]]
    ) -> dict[str, list[ValidationResult]]:
        results = {}
        for application in applications:
            endpoint = "database" if application == "target" else "postgresql"
            if application != missing_application:
                results[f"{application}/0"] = [result(endpoint=endpoint)]
            elif coverage == "skipped":
                results[f"{application}/0"] = [result(endpoint=endpoint, status="SKIPPED")]
            elif coverage == "wrong-endpoint":
                results[f"{application}/0"] = [result(endpoint="unselected")]
        return results

    client.validate_model.side_effect = validate
    # WHEN establishing baseline coverage, THEN skip before injecting any stress
    with pytest.raises(pytest.skip.Exception, match="No passing simple"):
        live.test_live_memory_stress_moderate(**arguments)
    chaos.check_stress.assert_not_called()
    chaos.stress_memory.assert_not_called()
