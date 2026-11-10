# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import MagicMock

import pytest
from chaos_client import MetaChaosClient, ResourceConstraintsClient
from chaos_client.backend import StressEndedEarlyError
from juju import CharmChannel, JujuApplicationInfo, JujuClient, JujuModelHandle
from kubernetes import client as k8s  # type: ignore[import-untyped]
from test_suite import test_live_memory_stress_total as module

MODEL = JujuModelHandle(controller="controller", model="model")
NEIGHBOR = JujuModelHandle(controller="other", model="neighbor")


def pod() -> k8s.V1Pod:
    return k8s.V1Pod(
        status=k8s.V1PodStatus(
            container_statuses=[
                k8s.V1ContainerStatus(
                    name="workload",
                    image="test",
                    image_id="id",
                    ready=True,
                    restart_count=0,
                )
            ]
        ),
        metadata=k8s.V1ObjectMeta(
            name="target-0",
            uid="after-rollout",
            annotations={"unit.juju.is/id": "target/0"},
            owner_references=[
                k8s.V1OwnerReference(
                    api_version="apps/v1", kind="StatefulSet", name="target", uid="sts", controller=True
                )
            ],
        ),
        spec=k8s.V1PodSpec(
            containers=[
                k8s.V1Container(
                    name="workload",
                    env=[k8s.V1EnvVar(name="JUJU_CONTAINER_NAME", value="workload")],
                    resources=k8s.V1ResourceRequirements(limits={"memory": "1Gi"}),
                )
            ]
        ),
    )


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "baseline",
        "stress",
        "hold",
        "cleanup",
        "recovery",
        "validation",
        "oom",
        "terminated-oom",
        "terminated-old-oom",
        "terminated-other-container",
        "terminated-other-exit",
        "replaced",
        "old-oom",
        "setup-oom",
        "other-container",
        "other-exit",
        "no-restart",
        "hold-and-cleanup",
    ],
)
@pytest.mark.parametrize("neighbor", [None, NEIGHBOR])
def test_lifecycle(failure: str | None, neighbor: JujuModelHandle | None, monkeypatch: pytest.MonkeyPatch) -> None:
    juju = MagicMock(spec=JujuClient, backend=MagicMock(), logger=MagicMock())
    juju.backend.list_applications.return_value = {
        "target": JujuApplicationInfo("postgresql-k8s", 495, CharmChannel.parse("14/stable"), "22.04")
    }
    kubernetes, chaos = MagicMock(), MagicMock()
    workload = pod()
    kubernetes.get_charm_pods.return_value = [workload]
    events: list[str] = []
    setup_started = datetime(2026, 9, 30, tzinfo=timezone.utc)
    confirmed_at = setup_started + timedelta(seconds=30)
    clock = MagicMock()
    clock.now.return_value = setup_started
    monkeypatch.setattr(module, "datetime", clock)

    def record(name: str) -> None:
        events.append(name)
        if name == "stress":
            clock.now.return_value = confirmed_at
        if failure == "hold-and-cleanup" and name in {"hold", "cleanup"}:
            raise RuntimeError(name)
        if failure == name:
            raise RuntimeError(name)

    @contextmanager
    def limit(*args: object) -> Iterator[None]:
        record("limit")
        try:
            yield
        finally:
            record("restore")

    def idle(**kwargs: object) -> None:
        assert kwargs["models"] == ([MODEL, neighbor] if neighbor else [MODEL])
        assert kwargs["strict_timeout"]
        name = "baseline" if not events else "recovery" if "cleanup" in events else "limited_baseline"
        record(name)

    def observe(seconds: float, oom_detected: Callable[[], bool]) -> None:
        assert seconds == 600
        record("hold")
        current_termination = failure is not None and failure.startswith("terminated-")
        evidence = failure.removeprefix("terminated-") if failure else None
        if evidence in {"oom", "replaced", "old-oom", "setup-oom", "other-container", "other-exit", "no-restart"}:
            workload.status = k8s.V1PodStatus(
                container_statuses=[
                    k8s.V1ContainerStatus(
                        name="other" if evidence == "other-container" else "workload",
                        image="test",
                        image_id="id",
                        ready=True,
                        restart_count=0 if current_termination or evidence == "no-restart" else 1,
                        last_state=k8s.V1ContainerState(
                            terminated=k8s.V1ContainerStateTerminated(
                                exit_code=1 if evidence == "other-exit" else 137,
                                reason="OOMKilled",
                                finished_at=(
                                    setup_started + timedelta(seconds=15)
                                    if evidence == "setup-oom"
                                    else confirmed_at - timedelta(days=1)
                                    if evidence == "old-oom"
                                    else confirmed_at + timedelta(seconds=1)
                                ),
                            )
                        ),
                    )
                ]
            )
        if current_termination:
            status = workload.status.container_statuses[0]
            status.state, status.last_state = status.last_state, None
        if failure == "replaced":
            workload.metadata.uid = "new-pod"
        assert oom_detected() is (failure in {"oom", "terminated-oom", "setup-oom"})

    monkeypatch.setattr(module, "temporary_memory_limit", limit)
    monkeypatch.setattr(
        module,
        "observe_memory_stress",
        lambda chaos, model, unit, seconds, *, oom_detected: observe(seconds, oom_detected),
    )
    juju.multi_model_idle_for_period.side_effect = idle
    juju.validate_model.side_effect = lambda **kw: record("validation")
    chaos.stress_memory.side_effect = lambda *a, **kw: record("stress")
    chaos.cleanup_all.side_effect = lambda: record("cleanup")

    def run() -> None:
        module.test_live_memory_stress_total(
            juju,
            lambda _: chaos,
            MODEL,
            "target",
            "1Gi",
            ResourceConstraintsClient(),
            timedelta(minutes=15),
            kubernetes,
            neighbor,
        )

    if failure == "hold-and-cleanup":
        with pytest.raises(RuntimeError, match="Memory stress failed") as error:
            run()
        assert "hold" in str(error.value)
        assert "cleanup" in str(error.value)
        assert str(error.value.__cause__) == "cleanup"
        assert events[-1] == "restore"
        chaos.cleanup_all.assert_called_once()
        juju.validate_model.assert_not_called()
    elif failure in {
        None,
        "terminated-oom",
        "terminated-old-oom",
        "terminated-other-container",
        "terminated-other-exit",
        "oom",
        "replaced",
        "old-oom",
        "setup-oom",
        "other-container",
        "other-exit",
        "no-restart",
    }:
        run()
        assert events[:7] == ["baseline", "limit", "limited_baseline", "stress", "hold", "cleanup", "recovery"]
        assert events[-2:] == ["restore", "recovery"]
        models = [MODEL, neighbor] if neighbor else [MODEL]
        assert [call.kwargs for call in juju.validate_model.call_args_list] == [
            {"model": m, "level": "deep"} for m in models
        ]
        chaos.stress_memory.assert_called_once_with(
            MODEL,
            "target/0",
            workers=1,
            size_mb=2048,
            duration=timedelta(seconds=600),
            duration_margin=timedelta(minutes=2),
        )
    else:
        with pytest.raises(RuntimeError, match=failure):
            run()
        if failure != "baseline":
            assert events[-1] == "restore"
            chaos.cleanup_all.assert_called_once()
        if failure in {"baseline", "stress", "hold", "cleanup", "recovery"}:
            juju.validate_model.assert_not_called()


@pytest.mark.parametrize("match", [True, False])
@pytest.mark.parametrize("configured_limit", [None, "2Gi"])
def test_yaml_settings_reach_stress_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, match: bool, configured_limit: str | None
) -> None:
    (tmp_path / "postgresql-k8s.yaml").write_text(
        "constraints:\n"
        "  - criteria:\n"
        "      - track: '14'\n"
        "        ubuntu_version: '22.04'\n"
        "    memory_exhaustion_workers: 2\n"
        "    memory_exhaustion_size_mb: 3072\n"
        "    memory_exhaustion_duration_seconds: 30\n"
        + (f"    memory_exhaustion_limit: '{configured_limit}'\n" if configured_limit is not None else "")
    )
    juju, kubernetes, chaos = MagicMock(), MagicMock(), MagicMock()
    juju.backend.list_applications.return_value = {
        "target": JujuApplicationInfo(
            "postgresql-k8s", 495, CharmChannel.parse("14/stable" if match else "16/stable"), "22.04"
        )
    }
    kubernetes.get_charm_pods.return_value = [pod()]
    limits: list[object] = []

    @contextmanager
    def limit(*args: object) -> Iterator[None]:
        limits.append(args[-1])
        resources = kubernetes.get_charm_pods.return_value[0].spec.containers[0].resources
        original = resources.limits["memory"]
        resources.limits["memory"] = args[-1]
        try:
            yield
        finally:
            resources.limits["memory"] = original

    monkeypatch.setattr(module, "temporary_memory_limit", limit)
    hold = MagicMock()
    monkeypatch.setattr(
        module, "observe_memory_stress", lambda chaos, model, unit, seconds, *, oom_detected: hold(seconds)
    )
    constraints = ResourceConstraintsClient(tmp_path)
    client = MetaChaosClient([chaos], juju.backend, constraints)
    module.test_live_memory_stress_total(
        juju,
        lambda _: client,
        MODEL,
        "target",
        "1Gi",
        constraints,
        timedelta(minutes=15),
        kubernetes,
        None,
    )
    chaos.stress_memory.assert_called_once_with(
        MODEL,
        "target/0",
        2 if match else 1,
        3072 if match else 2048,
        timedelta(seconds=150 if match else 720),
    )
    hold.assert_called_once_with(30 if match else 600)
    assert limits == [configured_limit if match and configured_limit is not None else "1Gi"]


@pytest.mark.parametrize("configured_limit", ["", "0", "-1Gi", "invalid", "NaN", "Infinity", "4Gi"])
def test_invalid_yaml_limit_fails_before_mutation(tmp_path: Path, configured_limit: str) -> None:
    # GIVEN an invalid limit or one exceeding the default stress size
    (tmp_path / "postgresql-k8s.yaml").write_text(f"constraints:\n  - memory_exhaustion_limit: '{configured_limit}'\n")
    juju, kubernetes, factory = MagicMock(), MagicMock(), MagicMock()
    juju.backend.list_applications.return_value = {
        "target": JujuApplicationInfo("postgresql-k8s", 495, CharmChannel.parse("14/stable"), "22.04")
    }

    # WHEN resolving configuration, THEN fail before touching workload resources
    with pytest.raises(ValueError):
        module.test_live_memory_stress_total(
            juju,
            factory,
            MODEL,
            "target",
            "1Gi",
            ResourceConstraintsClient(tmp_path),
            timedelta(minutes=15),
            kubernetes,
            None,
        )
    kubernetes.get_charm_pods.assert_not_called()
    factory.return_value.stress_memory.assert_not_called()


@pytest.mark.parametrize("kind", ["machine", "unsupported"])
def test_skip_before_mutation(kind: str) -> None:
    juju, kubernetes, factory = MagicMock(), MagicMock(), MagicMock()
    factory.return_value.supports.return_value = False
    with pytest.raises(pytest.skip.Exception):
        module.test_live_memory_stress_total(
            juju,
            factory,
            MODEL,
            "target",
            "1Gi",
            ResourceConstraintsClient(),
            timedelta(minutes=15),
            None if kind == "machine" else kubernetes,
            None,
        )
    if kind == "machine":
        factory.assert_not_called()
    else:
        factory.return_value.supports.assert_called_once_with("stress_memory")
    kubernetes.get_charm_pods.assert_not_called()


@pytest.mark.parametrize("problem", ["unknown-channel", "unknown-base", "insufficient-stress", "invalid-limit"])
def test_invalid_configuration_precedes_resource_mutation(problem: str) -> None:
    from chaos_client import CharmResourceConstraints

    juju, kubernetes, factory, config = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    juju.backend.list_applications.return_value = {
        "target": JujuApplicationInfo(
            "postgresql-k8s",
            495,
            None if problem == "unknown-channel" else CharmChannel.parse("14/stable"),
            None if problem == "unknown-base" else "22.04",
        )
    }
    config.get_charm_resource_constraints.return_value = CharmResourceConstraints(
        memory_exhaustion_size_mb=128 if problem == "insufficient-stress" else 2048
    )
    with pytest.raises((ValueError, pytest.fail.Exception)):
        module.test_live_memory_stress_total(
            juju,
            factory,
            MODEL,
            "target",
            "0" if problem == "invalid-limit" else "1Gi",
            config,
            timedelta(minutes=15),
            kubernetes,
            None,
        )
    kubernetes.get_charm_pods.assert_not_called()


@pytest.mark.parametrize("fail", [False, True])
def test_observation_polls_and_stops_on_error(monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    now = 0.0
    chaos = MagicMock()

    def pause(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(module, "monotonic", lambda: now)
    monkeypatch.setattr(module, "sleep", pause)
    if fail:
        chaos.check_stress.side_effect = [None, RuntimeError("injection failed")]
        with pytest.raises(RuntimeError, match="injection failed"):
            module.observe_memory_stress(chaos, MODEL, "target/0", 25, oom_detected=lambda: False)
        assert now == 10
    else:
        module.observe_memory_stress(chaos, MODEL, "target/0", 25, oom_detected=lambda: False)
        assert now == 25
        assert chaos.check_stress.call_count == 4


@pytest.mark.parametrize("experiment_error", [False, True])
def test_new_oom_ends_observation_without_suppressing_experiment_errors(
    monkeypatch: pytest.MonkeyPatch, experiment_error: bool
) -> None:
    chaos = MagicMock()
    pause = MagicMock()
    monkeypatch.setattr(module, "sleep", pause)
    if experiment_error:
        chaos.check_stress.side_effect = RuntimeError("helper failed")
        with pytest.raises(RuntimeError, match="helper failed"):
            module.observe_memory_stress(chaos, MODEL, "target/0", 600, oom_detected=lambda: True)
    else:
        module.observe_memory_stress(chaos, MODEL, "target/0", 600, oom_detected=lambda: True)
    chaos.check_stress.assert_called_once_with(MODEL, "target/0", allow_completed=True)
    pause.assert_not_called()


@pytest.mark.parametrize("new_oom", [False, True])
@pytest.mark.parametrize("helper_error", [False, True])
def test_oom_between_status_reads(new_oom: bool, helper_error: bool) -> None:
    chaos = MagicMock()
    early = StressEndedEarlyError("ended early")
    chaos.check_stress.side_effect = [early, RuntimeError("helper failed") if helper_error else None]
    evidence = MagicMock(side_effect=[False, new_oom])
    if not new_oom or helper_error:
        with pytest.raises(RuntimeError, match="helper failed" if new_oom else "ended early"):
            module.observe_memory_stress(chaos, MODEL, "target/0", 600, oom_detected=evidence)
    else:
        module.observe_memory_stress(chaos, MODEL, "target/0", 600, oom_detected=evidence)
    assert chaos.check_stress.call_count == (2 if new_oom else 1)
    if new_oom:
        chaos.check_stress.assert_called_with(MODEL, "target/0", allow_completed=True)
