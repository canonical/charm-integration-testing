# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from typing import cast

import pytest
from juju import JujuClient, JujuModelHandle
from test_suite import test_scale_remove_leader as scale_remove_leader

from bundle_builder_x import Charm, CharmChannel

MODEL = JujuModelHandle(controller="controller", model="model")
NEIGHBOR_MODEL = JujuModelHandle(controller="neighbor-controller", model="neighbor-model")


def _charm(*, ha_units: int = 3, scale_down: bool = True, subordinate: bool = False) -> Charm:
    return Charm(
        name="mysql-k8s",
        channel=CharmChannel.model_validate("8.0/stable"),
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["kubernetes"],
        subordinate=subordinate,
        ha_units=ha_units,
        scale_down=scale_down,
    )


class RecordingJujuClient:
    def __init__(
        self,
        current_units: int = 3,
        validation_error: Exception | None = None,
        removal_timeout_once: bool = False,
    ) -> None:
        self.current_units = current_units
        self.validation_error = validation_error
        self.removal_timeout_once = removal_timeout_once
        self.calls: list[tuple[object, ...]] = []

    def num_units(self, application: str, model: JujuModelHandle) -> int:
        self.calls.append(("num_units", application, model))
        return self.current_units

    def application_leader(self, application: str, model: JujuModelHandle) -> str:
        self.calls.append(("application_leader", application, model))
        return f"{application}/0"

    def remove_unit(self, unit: str, model: JujuModelHandle) -> None:
        self.calls.append(("remove_unit", unit, model))

    def wait_for_unit_removal(self, unit: str, model: JujuModelHandle, timeout: timedelta | None = None) -> None:
        self.calls.append(("wait_for_unit_removal", unit, model, timeout))
        if self.removal_timeout_once:
            self.removal_timeout_once = False
            raise TimeoutError("removal is still pending")
        self.current_units -= 1

    def idle_for_period(self, model: JujuModelHandle, timeout: timedelta | None = None) -> None:
        self.calls.append(("idle_for_period", model, timeout))

    def validate_model(
        self, model: JujuModelHandle, level: str = "simple", *, applications: list[str] | None = None
    ) -> None:
        self.calls.append(("validate_model", model, level, applications))
        if self.validation_error is not None:
            raise self.validation_error

    def scale_application(self, application: str, num: int, model: JujuModelHandle) -> None:
        self.calls.append(("scale_application", application, num, model))
        self.current_units = num

    def multi_model_idle_for_period(self, models: list[JujuModelHandle], timeout: timedelta | None = None) -> None:
        self.calls.append(("multi_model_idle_for_period", models, timeout))


def test_scale_remove_leader_validates_during_removal_and_restores_ha() -> None:
    client = RecordingJujuClient()

    scale_remove_leader.test_scale_remove_leader(
        cast(JujuClient, client),
        MODEL,
        "target",
        "machine",
        _charm(),
        None,
        None,
    )

    assert client.calls == [
        ("num_units", "target", MODEL),
        ("application_leader", "target", MODEL),
        ("remove_unit", "target/0", MODEL),
        ("wait_for_unit_removal", "target/0", MODEL, timedelta(minutes=15)),
        ("idle_for_period", MODEL, timedelta(minutes=15)),
        ("validate_model", MODEL, "deep", ["target"]),
        ("scale_application", "target", 3, MODEL),
        ("multi_model_idle_for_period", [MODEL], timedelta(minutes=15)),
    ]


def test_scale_remove_leader_restores_ha_when_validation_fails() -> None:
    validation_error = RuntimeError("validation failed")
    client = RecordingJujuClient(validation_error=validation_error)

    with pytest.raises(RuntimeError, match="validation failed"):
        scale_remove_leader.test_scale_remove_leader(
            cast(JujuClient, client),
            MODEL,
            "target",
            "machine",
            _charm(),
            None,
            None,
        )

    assert client.current_units == 3
    assert ("scale_application", "target", 3, MODEL) in client.calls


def test_scale_remove_leader_validates_target_and_neighbor_before_restoring() -> None:
    client = RecordingJujuClient()

    scale_remove_leader.test_scale_remove_leader(
        cast(JujuClient, client),
        MODEL,
        "target",
        "machine",
        _charm(),
        NEIGHBOR_MODEL,
        "neighbor",
    )

    assert client.calls[5:8] == [
        ("validate_model", MODEL, "deep", ["target"]),
        ("validate_model", NEIGHBOR_MODEL, "deep", ["neighbor"]),
        ("scale_application", "target", 3, MODEL),
    ]


def test_scale_remove_leader_waits_for_pending_removal_before_restoring_scale() -> None:
    client = RecordingJujuClient(removal_timeout_once=True)

    with pytest.raises(TimeoutError, match="removal is still pending"):
        scale_remove_leader.test_scale_remove_leader(
            cast(JujuClient, client),
            MODEL,
            "target",
            "machine",
            _charm(),
            None,
            None,
        )

    wait_indices = [i for i, call in enumerate(client.calls) if call[0] == "wait_for_unit_removal"]
    scale_index = next(i for i, call in enumerate(client.calls) if call[0] == "scale_application")
    assert len(wait_indices) == 2
    assert wait_indices[-1] < scale_index
    assert client.current_units == 3


def test_scale_remove_leader_skips_kubernetes_models() -> None:
    client = RecordingJujuClient()

    with pytest.raises(pytest.skip.Exception, match="does not support removing a specific unit"):
        scale_remove_leader.test_scale_remove_leader(
            cast(JujuClient, client),
            MODEL,
            "target",
            "kubernetes",
            _charm(),
            None,
            None,
        )

    assert client.calls == []
