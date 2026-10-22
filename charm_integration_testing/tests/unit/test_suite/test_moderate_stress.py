# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from traceback import extract_tb
from typing import Literal

import pytest
from chaos_client import ChaosCleanupError, CharmResourceConstraints, MetaChaosClient
from juju import (
    CharmChannel,
    JujuApplicationHealth,
    JujuApplicationInfo,
    JujuClient,
    JujuModelHandle,
    JujuValidationError,
)
from kubernetes_client import KubernetesClientRegistry
from test_suite import moderate_stress
from test_suite import test_live_cpu_stress_moderate as cpu
from test_suite import test_live_memory_stress_moderate as memory

from validators.base import ValidationResult

from ..chaos_client.test_meta_client import ClientStub, ConstraintsClientStub
from ..extensions.shared import NullJujuBackend

MODEL = JujuModelHandle(controller="controller", model="model")
NEIGHBOR = JujuModelHandle(controller="other-controller", model="neighbor-model")
UNIT = "target/2"


@dataclass
class Clock:
    now: float = 0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def result(
    status: Literal["PASS", "FAIL", "ERROR", "SKIPPED"] = "PASS",
    *,
    endpoint: str = "db",
    level: Literal["simple", "deep", "uat"] = "simple",
    interface: str = "database",
    relation_id: int = 1,
) -> ValidationResult:
    return ValidationResult(
        status=status, endpoint=endpoint, interface=interface, role="requires", level=level, relation_id=relation_id
    )


class Backend(NullJujuBackend):
    def kubernetes_cloud_for_model(self, model: JujuModelHandle) -> str | None:
        return None

    def __init__(self, clock: Clock, events: list[str]) -> None:
        self.clock = clock
        self.events = events
        self.units = ["target/7", UNIT]
        self.results: dict[str, list[dict[str, list[ValidationResult]]]] = {
            "target": [{UNIT: [result()]}],
            "neighbor": [{"neighbor/4": [result(endpoint="service")]}],
        }
        self.rounds: dict[str, int] = {}
        self.validation_seconds = 0.0
        self.idle_calls: list[list[JujuModelHandle]] = []
        self.health_calls: list[tuple[JujuModelHandle, str]] = []
        self.health_error_at: int | None = None
        self.recovery_error = False

    def list_applications(self, model: JujuModelHandle) -> dict[str, JujuApplicationInfo]:
        return {
            app: JujuApplicationInfo(charm=app, revision=1, channel=CharmChannel.parse("stable"), base="24.04")
            for app in self.results
            if (model == NEIGHBOR) == (app == "neighbor" and self.cross_model)
        }

    cross_model = False

    def application_units(self, model: JujuModelHandle, application: str) -> list[str]:
        assert model == MODEL and application == "target"
        return self.units

    def application_health(self, model: JujuModelHandle, application: str) -> JujuApplicationHealth:
        self.events.append(f"health:{application}")
        self.health_calls.append((model, application))
        if len(self.health_calls) == self.health_error_at:
            raise RuntimeError("health failed")
        return JujuApplicationHealth("active", {f"{application}/2": "active"}, {f"{application}/2": "executing"})

    def validate_application(
        self, model: JujuModelHandle, application: str, level: str
    ) -> dict[str, list[ValidationResult]]:
        assert level == "simple"
        assert model == (NEIGHBOR if application == "neighbor" and self.cross_model else MODEL)
        self.events.append(f"validate:{application}")
        round_number = self.rounds.get(application, 0)
        self.rounds[application] = round_number + 1
        if round_number:
            self.clock.now += self.validation_seconds
        choices = self.results[application]
        return choices[min(round_number, len(choices) - 1)]

    def wait_idle_multi_model(
        self,
        models: list[JujuModelHandle],
        timeout: timedelta | None,
        count: int | None,
        strict_timeout: bool = False,
    ) -> None:
        assert timeout == timedelta(minutes=15) and strict_timeout
        self.events.append("idle")
        self.idle_calls.append(models)
        if len(self.idle_calls) == 2 and self.recovery_error:
            raise TimeoutError("recovery failed")


class StressTool(ClientStub):
    def __init__(self, clock: Clock, events: list[str]) -> None:
        super().__init__({"stress_cpu", "stress_memory", "check_stress", "cleanup"})
        self.clock = clock
        self.events = events
        self.check_times: list[float] = []
        self.startup_seconds = 0.0
        self.check_error_at: int | None = None

    def stress_cpu(self, model: JujuModelHandle, unit: str, workers: int, duration: timedelta) -> None:
        self.events.append("inject")
        self.clock.now += self.startup_seconds
        super().stress_cpu(model, unit, workers, duration)

    def stress_memory(self, model: JujuModelHandle, unit: str, workers: int, size_mb: int, duration: timedelta) -> None:
        self.events.append("inject")
        self.clock.now += self.startup_seconds
        super().stress_memory(model, unit, workers, size_mb, duration)

    def check_stress(self, model: JujuModelHandle, unit: str, *, allow_completed: bool = False) -> None:
        self.events.append("stress")
        self.check_times.append(self.clock.now)
        if len(self.check_times) == self.check_error_at:
            raise RuntimeError("stress ended")
        super().check_stress(model, unit, allow_completed=allow_completed)

    def cleanup(self, model: JujuModelHandle, unit: str, path: str) -> None:
        self.events.append("cleanup")
        super().cleanup(model, unit, path)


@dataclass
class Harness:
    clock: Clock
    events: list[str]
    backend: Backend
    tool: StressTool
    constraints: ConstraintsClientStub
    chaos: MetaChaosClient
    resource: str

    @property
    def operation(self) -> str:
        return "stress_cpu" if self.resource == "cpu" else "stress_memory"

    def run(
        self,
        *,
        interval: timedelta = timedelta(seconds=10),
        margin: timedelta = timedelta(minutes=2),
        recovery: timedelta = timedelta(minutes=15),
        workers: int = 1,
        duration: timedelta = timedelta(minutes=5),
        size_mb: int = 128,
    ) -> None:
        def resolve(model: JujuModelHandle) -> MetaChaosClient:
            assert model == MODEL
            return self.chaos

        if self.resource == "cpu":
            cpu.test_live_cpu_stress_moderate(
                JujuClient(self.backend, logging.getLogger(__name__)),
                resolve,
                MODEL,
                "target",
                "db",
                NEIGHBOR if self.backend.cross_model else None,
                "neighbor",
                "service",
                interval,
                margin,
                recovery,
                workers,
                duration,
                KubernetesClientRegistry({}),
            )
        else:
            memory.test_live_memory_stress_moderate(
                JujuClient(self.backend, logging.getLogger(__name__)),
                resolve,
                MODEL,
                "target",
                "db",
                NEIGHBOR if self.backend.cross_model else None,
                "neighbor",
                "service",
                interval,
                recovery,
                workers,
                duration,
                size_mb,
                margin,
                KubernetesClientRegistry({}),
            )


@pytest.fixture(params=["cpu", "memory"])
def harness(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Harness:
    clock = Clock()
    monkeypatch.setattr(moderate_stress, "monotonic", clock.monotonic)
    monkeypatch.setattr(moderate_stress, "sleep", clock.sleep)
    events: list[str] = []
    backend = Backend(clock, events)
    tool = StressTool(clock, events)
    constraints = ConstraintsClientStub(
        CharmResourceConstraints(
            cpu_moderate_pressure_duration_seconds=20, memory_moderate_pressure_duration_seconds=20
        )
    )
    return Harness(
        clock, events, backend, tool, constraints, MetaChaosClient([tool], backend, constraints), request.param
    )


def test_caller_settings_reach_injection_without_charm_overrides(harness: Harness) -> None:
    # GIVEN caller-provided settings and no per-charm overrides
    harness.constraints.constraint = CharmResourceConstraints()
    # WHEN running either resource with non-default settings
    harness.run(workers=2, duration=timedelta(seconds=7), size_mb=64, margin=timedelta(seconds=15))
    # THEN the resource inputs reach injection and only the base duration is observed
    expected = (MODEL, UNIT, 2) + (() if harness.resource == "cpu" else (64,)) + (timedelta(seconds=22),)
    assert harness.tool.calls[0] == (harness.operation, expected)
    assert harness.tool.check_times[::2] == [0, 7]


@pytest.mark.parametrize("cross_model", [False, True], ids=["same-model", "cross-model"])
def test_lifecycle(harness: Harness, cross_model: bool) -> None:
    # GIVEN passing checks on both endpoints, including non-zero target unit numbers
    harness.backend.cross_model = cross_model

    # WHEN observing moderate pressure
    harness.run()

    # THEN every round is bracketed by active stress and health checks, without idle waits
    round_events = [
        "stress",
        "health:target",
        "health:neighbor",
        "validate:target",
        "validate:neighbor",
        "health:target",
        "health:neighbor",
        "stress",
    ]
    assert harness.events == [
        "idle",
        "health:target",
        "health:neighbor",
        "validate:target",
        "validate:neighbor",
        "inject",
        *round_events * 3,
        "cleanup",
        "idle",
        "health:target",
        "health:neighbor",
        "validate:target",
        "validate:neighbor",
    ]
    expected = (MODEL, UNIT, 1) + (() if harness.resource == "cpu" else (128,)) + (timedelta(seconds=140),)
    assert harness.tool.calls[0] == (harness.operation, expected)
    assert harness.tool.check_times == [0, 0, 10, 10, 20, 20]
    assert harness.backend.idle_calls == [[MODEL, NEIGHBOR] if cross_model else [MODEL]] * 2
    assert harness.backend.rounds == {"target": 5, "neighbor": 5}
    assert (NEIGHBOR if cross_model else MODEL, "neighbor") in harness.backend.health_calls


@pytest.mark.parametrize("cross_model", [False, True], ids=["same-model", "cross-model"])
@pytest.mark.parametrize("status", ["FAIL", "ERROR"])
def test_unrelated_validation_failure_is_excluded(
    harness: Harness, cross_model: bool, status: Literal["FAIL", "ERROR"]
) -> None:
    # GIVEN an unrelated failing application alongside the selected endpoints
    harness.backend.cross_model = cross_model
    harness.backend.results["unrelated"] = [{"unrelated/0": [result(status)]}]

    # WHEN running preflight, observation and recovery through the real JujuClient
    harness.run()

    # THEN only target and neighbor validators run in every phase
    assert harness.backend.rounds == {"target": 5, "neighbor": 5}
    assert harness.events.count("cleanup") == 1


def test_duplicate_endpoints_validate_application_once(harness: Harness) -> None:
    # GIVEN two endpoints belonging to the same application
    harness.backend.results["target"] = [{UNIT: [result(), result(endpoint="other")]}]
    client = JujuClient(harness.backend, logging.getLogger(__name__))

    # WHEN collecting coverage, THEN both endpoints share one application validation
    passed = moderate_stress.validate_service(client, [(MODEL, "target", "db"), (MODEL, "target", "other")])

    assert passed == {(MODEL, UNIT, endpoint, "database", 1) for endpoint in ("db", "other")}
    assert harness.backend.rounds == {"target": 1}


@pytest.mark.parametrize("cross_model", [False, True], ids=["same-model", "cross-model"])
@pytest.mark.parametrize("status", ["FAIL", "ERROR"])
def test_unselected_endpoint_failure_is_excluded(
    harness: Harness, cross_model: bool, status: Literal["FAIL", "ERROR"]
) -> None:
    # GIVEN failures on endpoints selected only for the other application
    harness.backend.cross_model = cross_model
    harness.backend.results["target"] = [{UNIT: [result(), result(status, endpoint="service")]}]
    harness.backend.results["neighbor"] = [{"neighbor/4": [result(endpoint="service"), result(status)]}]

    # WHEN running all phases, THEN filtering uses both application and endpoint
    harness.run()
    assert harness.backend.rounds == {"target": 5, "neighbor": 5}
    assert harness.events.count("cleanup") == 1


@dataclass(frozen=True)
class DurationParams:
    label: str
    seconds: int | None
    workers: int | None
    interval: int
    margin: int
    expected_times: list[float]


@pytest.mark.parametrize(
    "params",
    [
        DurationParams("defaults", None, None, 100, 120, [0, 100, 200, 300]),
        DurationParams("short-override", 7, 2, 10, 30, [0, 7]),
        DurationParams("long-override", 600, 3, 250, 40, [0, 250, 500, 600]),
    ],
    ids=lambda params: params.label,
)
def test_resolved_duration_and_margin(harness: Harness, params: DurationParams) -> None:
    # GIVEN moderate settings distinct from exhaustion settings
    harness.constraints.constraint = CharmResourceConstraints(
        cpu_moderate_pressure_workers=params.workers,
        cpu_moderate_pressure_duration_seconds=params.seconds,
        memory_moderate_pressure_workers=params.workers,
        memory_moderate_pressure_duration_seconds=params.seconds,
        memory_exhaustion_workers=99,
        memory_exhaustion_duration_seconds=999,
        cpu_exhaustion_workers=99,
        cpu_exhaustion_duration_seconds=999,
    )

    # WHEN the real Meta client resolves and injects pressure
    harness.run(interval=timedelta(seconds=params.interval), margin=timedelta(seconds=params.margin))

    # THEN overrides determine observation, with the margin added only to injection
    assert harness.tool.calls[0] == (
        harness.operation,
        (MODEL, UNIT, params.workers or 1)
        + (() if harness.resource == "cpu" else (128,))
        + (timedelta(seconds=(params.seconds or 300) + params.margin),),
    )
    assert harness.tool.check_times[::2] == params.expected_times
    assert harness.constraints.calls == [("target", "stable", "24.04")]


@pytest.mark.parametrize(
    "validations",
    [[], [result("SKIPPED")], [result(endpoint="other")], [result(level="deep")]],
    ids=["empty", "skipped", "unrelated-endpoint", "not-simple"],
)
def test_no_preflight_coverage_skips_before_injection(harness: Harness, validations: list[ValidationResult]) -> None:
    # GIVEN no passing simple validator for either tested endpoint
    harness.backend.results = {"target": [{UNIT: validations}], "neighbor": [{}]}

    # WHEN preflight runs, THEN it skips before injecting
    with pytest.raises(pytest.skip.Exception, match="No passing simple interface validators"):
        harness.run()
    assert "inject" not in harness.events


@pytest.mark.parametrize("cross_model", [False, True], ids=["same-model", "cross-model"])
@pytest.mark.parametrize("application", ["target", "neighbor"])
@pytest.mark.parametrize("coverage", ["empty", "skipped", "other-endpoint", "deep"])
def test_partial_baseline_skips_before_injection(
    harness: Harness, cross_model: bool, application: str, coverage: str
) -> None:
    # GIVEN just one selected endpoint lacks a passing simple result
    harness.backend.cross_model = cross_model
    unit = UNIT if application == "target" else "neighbor/4"
    endpoint = "db" if application == "target" else "service"
    validations = {
        "empty": [],
        "skipped": [result("SKIPPED", endpoint=endpoint)],
        "other-endpoint": [result(endpoint="unselected")],
        "deep": [result(endpoint=endpoint, level="deep")],
    }[coverage]
    harness.backend.results[application] = [{unit: validations}]

    # WHEN preflight runs, THEN the other endpoint's PASS cannot hide the gap
    with pytest.raises(pytest.skip.Exception, match="No passing simple interface validators"):
        harness.run()
    assert "inject" not in harness.events
    assert harness.tool.check_times == []


@dataclass(frozen=True)
class CoverageParams:
    label: str
    results: dict[str, list[ValidationResult]]


@pytest.mark.parametrize(
    "params",
    [
        CoverageParams("missing", {}),
        CoverageParams("skipped", {UNIT: [result("SKIPPED")]}),
        CoverageParams("changed-unit", {"target/7": [result()]}),
        CoverageParams("changed-relation", {UNIT: [result(relation_id=2)]}),
        CoverageParams("changed-interface", {UNIT: [result(interface="different")]}),
    ],
    ids=lambda params: params.label,
)
@pytest.mark.parametrize("phase", ["observation", "recovery"])
def test_lost_coverage_fails(harness: Harness, params: CoverageParams, phase: str) -> None:
    # GIVEN preflight passes but a required check later disappears
    baseline = {UNIT: [result()]}
    harness.backend.results["target"] = [baseline] * (1 if phase == "observation" else 4) + [params.results]

    # WHEN checking coverage, THEN missing/skipped checks fail, not skip
    with pytest.raises(RuntimeError, match="coverage disappeared"):
        harness.run()
    assert harness.events.count("cleanup") == 1


@pytest.mark.parametrize("status", ["FAIL", "ERROR"])
@pytest.mark.parametrize("phase", ["preflight", "observation", "recovery"])
def test_validation_failures_propagate(harness: Harness, status: Literal["FAIL", "ERROR"], phase: str) -> None:
    # GIVEN a validator failure at the selected phase
    before = {"preflight": 0, "observation": 1, "recovery": 4}[phase]
    harness.backend.results["neighbor"] = [{"neighbor/4": [result(endpoint="service")]}] * before + [
        {"neighbor/4": [result(status, endpoint="service")]}
    ]

    # WHEN running the test, THEN real JujuClient failures propagate
    with pytest.raises(JujuValidationError):
        harness.run()
    assert harness.events.count("inject") == (phase != "preflight")
    assert harness.events.count("cleanup") == (phase != "preflight")


@pytest.mark.parametrize("check", [1, 2], ids=["before-round", "after-round"])
def test_stress_must_remain_active(harness: Harness, check: int) -> None:
    # GIVEN the selected tool loses injection before/after a round
    harness.tool.check_error_at = check

    # WHEN observing, THEN it fails and cleans up without claiming recovery
    with pytest.raises(RuntimeError, match="stress ended"):
        harness.run()
    assert harness.events[-1] == "cleanup"
    assert len(harness.backend.idle_calls) == 1


@pytest.mark.parametrize("check", [1, 3, 5, 15], ids=["preflight", "before-round", "after-round", "recovery"])
def test_health_errors_propagate(harness: Harness, check: int) -> None:
    # GIVEN a status failure at a sampled phase
    harness.backend.health_error_at = check

    # WHEN checking health, THEN errors fail rather than skip
    with pytest.raises(RuntimeError, match="health failed"):
        harness.run()
    assert harness.events.count("cleanup") == (check != 1)


@pytest.mark.parametrize("operation", ["inject", "check_stress"], ids=["injection", "observation"])
@pytest.mark.parametrize("also_cleanup", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_failure_always_cleans_up_and_preserves_errors(
    harness: Harness, operation: str, also_cleanup: bool, error_type: type[BaseException]
) -> None:
    # GIVEN injection or observation fails, optionally followed by cleanup failure
    operation = harness.operation if operation == "inject" else operation
    error = error_type(f"{operation} failed")
    harness.tool.errors[operation] = error
    if also_cleanup:
        harness.tool.errors["cleanup"] = RuntimeError("cleanup failed")

    # WHEN running, THEN cleanup occurs and neither error is lost
    with pytest.raises(error_type) as caught:
        harness.run()
    assert caught.value is error
    if also_cleanup:
        cleanup_error = caught.value.__cause__
        assert isinstance(cleanup_error, ChaosCleanupError)
        assert cleanup_error.errors[0] is harness.tool.errors["cleanup"]
        assert cleanup_error.__traceback__ is not None
    assert harness.events[-1] == "cleanup"
    assert len(harness.backend.idle_calls) == 1


def test_cleanup_preserves_observation_type_and_traceback(harness: Harness) -> None:
    harness.tool.check_error_at = 1
    harness.tool.errors["cleanup"] = ValueError("cleanup failed")

    with pytest.raises(RuntimeError, match="stress ended") as caught:
        harness.run()

    assert type(caught.value) is RuntimeError
    assert isinstance(caught.value.__cause__, ChaosCleanupError)
    assert any(frame.name == "check_stress" for frame in extract_tb(caught.value.__traceback__))
    assert harness.events[-1] == "cleanup"


def test_cleanup_failure_blocks_recovery(harness: Harness) -> None:
    # GIVEN successful observation but failed cleanup
    harness.tool.errors["cleanup"] = RuntimeError("cleanup failed")

    # WHEN cleanup runs, THEN recovery is not claimed
    with pytest.raises(ChaosCleanupError):
        harness.run()
    assert len(harness.backend.idle_calls) == 1


@pytest.mark.parametrize("overrun", [0, 1], ids=["at-budget", "past-budget"])
def test_validator_overrun_fails(harness: Harness, overrun: int) -> None:
    # GIVEN synchronous validation takes the entire injection budget or longer
    harness.backend.validation_seconds = 70 + overrun

    # WHEN the validator returns, THEN even a tool reporting active cannot hide the overrun
    with pytest.raises(TimeoutError, match="execution budget"):
        harness.run()
    assert harness.tool.check_times == [0, 140 + 2 * overrun]
    assert harness.events[-1] == "cleanup"


def test_startup_consumes_margin(harness: Harness) -> None:
    # GIVEN startup uses the entire conservative stress budget
    harness.tool.startup_seconds = 140

    # WHEN injection returns, THEN observation cannot pretend it has a fresh budget
    with pytest.raises(TimeoutError, match="execution budget"):
        harness.run()
    assert harness.tool.check_times == []
    assert harness.events[-1] == "cleanup"


def test_final_round_can_use_margin(harness: Harness) -> None:
    # GIVEN each endpoint validator takes three seconds
    harness.backend.validation_seconds = 3

    # WHEN a final round extends beyond observation, THEN it can finish inside the margin
    harness.run()
    assert harness.tool.check_times == [0, 6, 16, 22]
    assert harness.clock.sleeps == [10]


@pytest.mark.parametrize("value", [timedelta(0), timedelta(seconds=-1)])
@pytest.mark.parametrize("setting", ["interval", "margin", "recovery"])
def test_positive_configuration_required(harness: Harness, setting: str, value: timedelta) -> None:
    # GIVEN an invalid fixture setting
    # WHEN running, THEN configuration fails before preflight/injection
    with pytest.raises(ValueError, match="must be positive"):
        harness.run(
            interval=value if setting == "interval" else timedelta(seconds=10),
            margin=value if setting == "margin" else timedelta(minutes=2),
            recovery=value if setting == "recovery" else timedelta(minutes=15),
        )
    assert harness.events == []


def test_unsupported_skips_without_fallback(harness: Harness) -> None:
    # GIVEN tools support other chaos operations only
    harness.tool.supported = {"fill_disk"}

    # WHEN CPU stress is requested, THEN there is no native fallback or injection
    with pytest.raises(pytest.skip.Exception, match=harness.operation):
        harness.run()
    assert harness.events == []
    assert harness.constraints.calls == []


def test_no_target_units_fails_before_injection(harness: Harness) -> None:
    # GIVEN no real target units
    harness.backend.units = []

    # WHEN selecting a unit, THEN no fabricated /0 is injected
    with pytest.raises(pytest.fail.Exception, match="No target units"):
        harness.run()
    assert "inject" not in harness.events


def test_recovery_timeout_propagates(harness: Harness) -> None:
    # GIVEN stress has been removed but the model cannot recover unaided
    harness.backend.recovery_error = True

    # WHEN waiting for recovery, THEN no restart or success is substituted
    with pytest.raises(TimeoutError, match="recovery failed"):
        harness.run()
    assert harness.events[-2:] == ["cleanup", "idle"]
