# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from juju import JujuClient, JujuModelHandle
from test_suite import conftest as suite_conftest
from test_suite import test_scale_ha as scale_ha

from bundle_builder_x import Charm, CharmChannel


class RecordingJujuClient:
    def __init__(self, current_units: int) -> None:
        self.current_units = current_units
        self.calls: list[tuple[object, ...]] = []

    def num_units(self, application: str, model: JujuModelHandle) -> int:
        self.calls.append(("num_units", application, model))
        return self.current_units

    def scale_application(self, application: str, num: int, model: JujuModelHandle) -> None:
        self.calls.append(("scale_application", application, num, model))

    def idle_for_period(self, model: JujuModelHandle, timeout: timedelta | None = None) -> None:
        self.calls.append(("idle_for_period", model, timeout))

    def validate_model(self, model: JujuModelHandle, level: str = "simple") -> None:
        self.calls.append(("validate_model", model, level))


MODEL = JujuModelHandle(controller="controller", model="model")


def _charm(*, ha_units: int = 3, scale_down: bool = True) -> Charm:
    return Charm(
        name="mysql-k8s",
        channel=CharmChannel.model_validate("8.0/stable"),
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["kubernetes"],
        ha_units=ha_units,
        scale_down=scale_down,
    )


def test_scale_to_ha_validates_immediately_when_application_is_already_large_enough() -> None:
    client = RecordingJujuClient(current_units=4)

    scale_ha.test_scale_to_ha(cast(JujuClient, client), MODEL, "target", _charm())

    assert client.calls == [
        ("num_units", "target", MODEL),
        ("validate_model", MODEL, "deep"),
    ]


def test_scale_to_ha_scales_waits_and_deep_validates() -> None:
    client = RecordingJujuClient(current_units=1)

    scale_ha.test_scale_to_ha(cast(JujuClient, client), MODEL, "target", _charm(ha_units=5))

    assert client.calls == [
        ("num_units", "target", MODEL),
        ("scale_application", "target", 5, MODEL),
        ("idle_for_period", MODEL, timedelta(minutes=15)),
        ("validate_model", MODEL, "deep"),
    ]


def test_scale_from_ha_restores_original_units_waits_and_simple_validates() -> None:
    client = RecordingJujuClient(current_units=3)

    scale_ha.test_scale_from_ha(
        cast(JujuClient, client), MODEL, "target", original_units=2, target_deployed_charm=_charm()
    )

    assert client.calls == [
        ("scale_application", "target", 2, MODEL),
        ("idle_for_period", MODEL, timedelta(minutes=15)),
        ("validate_model", MODEL, "simple"),
    ]


def test_scale_from_ha_skips_before_mutating_when_scaling_down_is_unsupported() -> None:
    client = RecordingJujuClient(current_units=3)

    with pytest.raises(pytest.skip.Exception, match="mysql-k8s does not support scaling down"):
        scale_ha.test_scale_from_ha(
            cast(JujuClient, client),
            MODEL,
            "target",
            original_units=2,
            target_deployed_charm=_charm(scale_down=False),
        )

    assert client.calls == []


def test_bundle_application_units_reads_platform_specific_unit_key(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        "applications:\n"
        "  target:\n"
        "    scale: 2\n"
        "  machine-target:\n"
        "    num_units: 4\n"
        "---\n"
        "applications:\n"
        "  target:\n"
        "    offers: {}\n",
        encoding="utf-8",
    )

    assert suite_conftest._bundle_application_units(bundle, "target", "kubernetes") == 2
    assert suite_conftest._bundle_application_units(bundle, "machine-target", "machine") == 4
