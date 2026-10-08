# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from typing import cast

import pytest
from juju import JujuClient, JujuModelHandle
from test_suite import test_reboot_leader as reboot_leader

from bundle_builder_x import Charm, CharmChannel

MODEL = JujuModelHandle(controller="controller", model="model")


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
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def num_units(self, application: str, model: JujuModelHandle) -> int:
        self.calls.append(("num_units", application, model))
        return 3

    def application_leader(self, application: str, model: JujuModelHandle) -> str:
        self.calls.append(("application_leader", application, model))
        return f"{application}/1"

    def ssh(self, unit: str, command: str, model: JujuModelHandle) -> None:
        self.calls.append(("ssh", unit, command, model))

    def validate_model(
        self, model: JujuModelHandle, level: str = "simple", *, applications: list[str] | None = None
    ) -> None:
        self.calls.append(("validate_model", model, level, applications))

    def multi_model_idle_for_period(self, models: list[JujuModelHandle], timeout: timedelta | None = None) -> None:
        self.calls.append(("multi_model_idle_for_period", models, timeout))


def test_reboot_leader_validates_before_waiting_for_recovery() -> None:
    client = RecordingJujuClient()

    reboot_leader.test_reboot_leader(
        cast(JujuClient, client),
        MODEL,
        "target",
        "machine",
        _charm(),
        None,
        None,
        None,
    )

    assert client.calls == [
        ("num_units", "target", MODEL),
        ("application_leader", "target", MODEL),
        ("ssh", "target/1", "sudo reboot", MODEL),
        ("validate_model", MODEL, "deep", ["target"]),
        ("multi_model_idle_for_period", [MODEL], timedelta(minutes=15)),
    ]


def test_reboot_leader_waits_for_recovery_when_validation_fails() -> None:
    validation_error = RuntimeError("validation failed")

    class FailingValidationClient(RecordingJujuClient):
        def validate_model(
            self, model: JujuModelHandle, level: str = "simple", *, applications: list[str] | None = None
        ) -> None:
            super().validate_model(model, level, applications=applications)
            raise validation_error

    client = FailingValidationClient()

    with pytest.raises(RuntimeError, match="validation failed"):
        reboot_leader.test_reboot_leader(
            cast(JujuClient, client),
            MODEL,
            "target",
            "machine",
            _charm(),
            None,
            None,
            None,
        )

    assert client.calls[-1] == ("multi_model_idle_for_period", [MODEL], timedelta(minutes=15))
