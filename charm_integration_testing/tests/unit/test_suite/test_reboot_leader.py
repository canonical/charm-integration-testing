# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from typing import cast

import pytest
from juju import JujuClient, JujuModelHandle
from kubernetes_client import KubernetesClientRegistry
from test_suite import test_reboot_leader as reboot_leader

from bundle_builder_x import Charm, CharmChannel

from .test_kubernetes_access import CloudBackend

MODEL = JujuModelHandle(controller="controller", model="model")
NEIGHBOR_MODEL = JujuModelHandle(controller="neighbor-controller", model="neighbor-model")


def _charm(*, ha_units: int = 3, subordinate: bool = False) -> Charm:
    return Charm(
        name="mysql-k8s",
        channel=CharmChannel.model_validate("8.0/stable"),
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["machine"],
        subordinate=subordinate,
        ha_units=ha_units,
    )


class RecordingJujuClient:
    def __init__(
        self,
        reboot_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.backend = CloudBackend({MODEL.uri: None})
        self.reboot_error = reboot_error

    def num_units(self, application: str, model: JujuModelHandle) -> int:
        self.calls.append(("num_units", application, model))
        return 3

    def application_leader(self, application: str, model: JujuModelHandle) -> str:
        self.calls.append(("application_leader", application, model))
        return f"{application}/1"

    def application_units(self, application: str, model: JujuModelHandle) -> list[str]:
        self.calls.append(("application_units", application, model))
        return [f"{application}/0", f"{application}/1", f"{application}/2"]

    def reboot_unit(self, unit: str, model: JujuModelHandle) -> None:
        self.calls.append(("reboot_unit", unit, model))
        if self.reboot_error is not None:
            raise self.reboot_error

    def wait_for_unit_unavailable(self, unit: str, model: JujuModelHandle, timeout: timedelta | None = None) -> None:
        self.calls.append(("wait_for_unit_unavailable", unit, model, timeout))

    def wait_for_unit_available(self, unit: str, model: JujuModelHandle, timeout: timedelta | None = None) -> None:
        self.calls.append(("wait_for_unit_available", unit, model, timeout))

    def wait_for_unit_restart_completion(
        self, unit: str, model: JujuModelHandle, timeout: timedelta | None = None
    ) -> None:
        self.calls.append(("wait_for_unit_restart_completion", unit, model, timeout))

    def validate_model(
        self,
        model: JujuModelHandle,
        level: str = "simple",
        *,
        applications: list[str] | None = None,
        excluded_units: set[str] | None = None,
    ) -> None:
        self.calls.append(("validate_model", model, level, applications, excluded_units))

    def multi_model_idle_for_period(self, models: list[JujuModelHandle], timeout: timedelta | None = None) -> None:
        self.calls.append(("multi_model_idle_for_period", models, timeout))


def test_reboot_leader_validates_before_waiting_for_recovery() -> None:
    client = RecordingJujuClient()

    reboot_leader.test_reboot_leader(
        cast(JujuClient, client),
        MODEL,
        "target",
        _charm(),
        None,
        None,
        KubernetesClientRegistry({}),
    )

    assert client.calls == [
        ("num_units", "target", MODEL),
        ("application_leader", "target", MODEL),
        ("application_units", "target", MODEL),
        ("reboot_unit", "target/1", MODEL),
        ("wait_for_unit_unavailable", "target/1", MODEL, timedelta(minutes=15)),
        ("validate_model", MODEL, "deep", ["target"], {"target/1"}),
        ("wait_for_unit_restart_completion", "target/1", MODEL, timedelta(minutes=15)),
        ("multi_model_idle_for_period", [MODEL], timedelta(minutes=15)),
        ("validate_model", MODEL, "deep", ["target"], {"target/0", "target/2"}),
    ]


def test_reboot_leader_waits_for_recovery_when_validation_fails() -> None:
    validation_error = RuntimeError("validation failed")

    class FailingValidationClient(RecordingJujuClient):
        def validate_model(
            self,
            model: JujuModelHandle,
            level: str = "simple",
            *,
            applications: list[str] | None = None,
            excluded_units: set[str] | None = None,
        ) -> None:
            super().validate_model(model, level, applications=applications, excluded_units=excluded_units)
            raise validation_error

    client = FailingValidationClient()

    with pytest.raises(RuntimeError, match="validation failed"):
        reboot_leader.test_reboot_leader(
            cast(JujuClient, client),
            MODEL,
            "target",
            _charm(),
            None,
            None,
            KubernetesClientRegistry({}),
        )

    assert client.calls[-1] == ("multi_model_idle_for_period", [MODEL], timedelta(minutes=15))


def test_reboot_leader_waits_for_unavailability_before_recovery_after_reboot_error() -> None:
    reboot_error = RuntimeError("reboot request failed after acceptance")
    client = RecordingJujuClient(reboot_error=reboot_error)

    with pytest.raises(RuntimeError, match="reboot request failed after acceptance") as error:
        reboot_leader.test_reboot_leader(
            cast(JujuClient, client),
            MODEL,
            "target",
            _charm(),
            None,
            None,
            KubernetesClientRegistry({}),
        )

    assert error.value is reboot_error
    assert client.calls[4:] == [
        ("wait_for_unit_unavailable", "target/1", MODEL, timedelta(minutes=15)),
        ("wait_for_unit_restart_completion", "target/1", MODEL, timedelta(minutes=15)),
        ("multi_model_idle_for_period", [MODEL], timedelta(minutes=15)),
    ]


def test_reboot_leader_validates_same_model_neighbor() -> None:
    client = RecordingJujuClient()

    reboot_leader.test_reboot_leader(
        cast(JujuClient, client),
        MODEL,
        "target",
        _charm(),
        None,
        "neighbor",
        KubernetesClientRegistry({}),
    )

    assert ("validate_model", MODEL, "deep", ["target"], {"target/1"}) in client.calls
    assert ("validate_model", MODEL, "deep", ["neighbor"], None) in client.calls


def test_reboot_leader_validates_neighbor_model_and_recovers() -> None:
    juju = RecordingJujuClient()

    reboot_leader.test_reboot_leader(
        cast(JujuClient, juju),
        MODEL,
        "target",
        _charm(),
        NEIGHBOR_MODEL,
        "neighbor",
        KubernetesClientRegistry({}),
    )

    assert juju.calls == [
        ("num_units", "target", MODEL),
        ("application_leader", "target", MODEL),
        ("application_units", "target", MODEL),
        ("reboot_unit", "target/1", MODEL),
        ("wait_for_unit_unavailable", "target/1", MODEL, timedelta(minutes=15)),
        ("validate_model", MODEL, "deep", ["target"], {"target/1"}),
        ("validate_model", NEIGHBOR_MODEL, "deep", ["neighbor"], None),
        ("wait_for_unit_restart_completion", "target/1", MODEL, timedelta(minutes=15)),
        ("multi_model_idle_for_period", [MODEL, NEIGHBOR_MODEL], timedelta(minutes=15)),
        ("validate_model", MODEL, "deep", ["target"], {"target/0", "target/2"}),
    ]


def test_reboot_leader_waits_for_disruption_after_reboot_error() -> None:
    reboot_error = RuntimeError("pod deletion accepted then failed")
    client = RecordingJujuClient(reboot_error=reboot_error)

    with pytest.raises(RuntimeError, match="pod deletion accepted then failed") as error:
        reboot_leader.test_reboot_leader(
            cast(JujuClient, client),
            MODEL,
            "target",
            _charm(),
            None,
            None,
            KubernetesClientRegistry({}),
        )

    assert error.value is reboot_error
    assert ("wait_for_unit_unavailable", "target/1", MODEL, timedelta(minutes=15)) in client.calls
