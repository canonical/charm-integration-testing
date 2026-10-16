# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

from juju import JujuModelHandle

from bundle_builder_x import Charm, CharmChannel

MODEL = JujuModelHandle(controller="controller", model="model")


def charm(*, ha_units: int = 3, scale_down: bool = True, subordinate: bool = False) -> Charm:
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
    def __init__(self, current_units: int) -> None:
        self.units = [f"target/{index}" for index in range(current_units)]
        self.calls: list[tuple[object, ...]] = []

    def num_units(self, application: str, model: JujuModelHandle) -> int:
        self.calls.append(("num_units", application, model))
        return len(self.units)

    def scale_application(self, application: str, num: int, model: JujuModelHandle) -> None:
        self.calls.append(("scale_application", application, num, model))
        self.units = [f"{application}/{index}" for index in range(num)]

    def idle_for_period(self, model: JujuModelHandle, timeout: timedelta | None = None) -> None:
        self.calls.append(("idle_for_period", model, timeout))

    def multi_model_idle_for_period(
        self,
        models: list[JujuModelHandle],
        timeout: timedelta | None = None,
        strict_timeout: bool = False,
    ) -> None:
        self.calls.append(("idle", models, strict_timeout))

    def validate_model(self, model: JujuModelHandle, level: str = "simple") -> None:
        self.calls.append(("validate_model", model, level))
