# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from juju import JujuClient, JujuModelHandle, JujuRestartNotSupportedError
from test_suite import test_scale_ha as scale_ha
from test_suite import test_scale_remove_follower as remove_follower

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


def _write_bundle(tmp_path: Path, *, application: str = "target", units: int = 2) -> Path:
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(f"applications:\n  {application}:\n    scale: {units}\n", encoding="utf-8")
    return bundle


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


def test_scale_to_ha_skips_subordinate_before_accessing_juju() -> None:
    client = RecordingJujuClient(current_units=1)

    with pytest.raises(pytest.skip.Exception, match="mysql-k8s is subordinate"):
        scale_ha.test_scale_to_ha(
            cast(JujuClient, client),
            MODEL,
            "target",
            _charm(subordinate=True),
        )

    assert client.calls == []


def test_scale_from_ha_restores_original_units_waits_and_simple_validates(tmp_path: Path) -> None:
    client = RecordingJujuClient(current_units=3)

    scale_ha.test_scale_from_ha(
        cast(JujuClient, client),
        MODEL,
        _write_bundle(tmp_path),
        "target",
        "kubernetes",
        _charm(),
    )

    assert client.calls == [
        ("scale_application", "target", 2, MODEL),
        ("idle_for_period", MODEL, timedelta(minutes=15)),
        ("validate_model", MODEL, "simple"),
    ]


def test_scale_from_ha_skips_before_parsing_or_mutating_when_scaling_down_is_unsupported(tmp_path: Path) -> None:
    client = RecordingJujuClient(current_units=3)

    with pytest.raises(pytest.skip.Exception, match="mysql-k8s does not support scaling down"):
        scale_ha.test_scale_from_ha(
            cast(JujuClient, client),
            MODEL,
            tmp_path / "missing.yaml",
            "target",
            "kubernetes",
            _charm(scale_down=False),
        )

    assert client.calls == []


def test_scale_from_ha_skips_subordinate_before_parsing_or_accessing_juju(tmp_path: Path) -> None:
    client = RecordingJujuClient(current_units=3)

    with pytest.raises(pytest.skip.Exception, match="mysql-k8s is subordinate"):
        scale_ha.test_scale_from_ha(
            cast(JujuClient, client),
            MODEL,
            tmp_path / "missing.yaml",
            "target",
            "kubernetes",
            _charm(subordinate=True),
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

    assert scale_ha._bundle_application_units(bundle, "target", "kubernetes") == 2
    assert scale_ha._bundle_application_units(bundle, "machine-target", "machine") == 4


class FollowerClient(RecordingJujuClient):
    def __init__(self, *, error: str | None = None, current_units: int = 3) -> None:
        super().__init__(current_units)
        self.error = error

    def multi_model_idle_for_period(
        self, models: list[JujuModelHandle], timeout: timedelta, strict_timeout: bool
    ) -> None:
        self.calls.append(("idle", models, strict_timeout))
        if self.error == "recovery" and any(call[0] == "restart" for call in self.calls):
            raise TimeoutError("recovery")

    def restart_follower(self, application: str, model: JujuModelHandle, timeout: timedelta) -> str:
        self.calls.append(("restart", application, model))
        if self.error == "restart":
            raise RuntimeError("restart")
        if self.error == "unsupported":
            raise JujuRestartNotSupportedError("unsupported")
        if self.error == "not-implemented":
            raise NotImplementedError("restart hook failed")
        if self.error == "count":
            self.current_units -= 1
        return "target/1"

    def validate_model(self, model: JujuModelHandle, level: str = "simple") -> None:
        super().validate_model(model, level)
        if self.error == "validation":
            raise RuntimeError("validation")


def test_follower_restart_preserves_ha_without_requiring_scale_down() -> None:
    client = FollowerClient()
    neighbor = JujuModelHandle(controller="controller", model="neighbor")
    remove_follower.test_scale_remove_follower(
        cast(JujuClient, client), MODEL, neighbor, "target", _charm(scale_down=False)
    )
    assert client.calls == [
        ("idle", [MODEL, neighbor], True),
        ("num_units", "target", MODEL),
        ("restart", "target", MODEL),
        ("idle", [MODEL, neighbor], True),
        ("num_units", "target", MODEL),
        ("validate_model", MODEL, "simple"),
        ("validate_model", neighbor, "simple"),
    ]


@pytest.mark.parametrize("error", ["restart", "recovery", "validation", "count"])
def test_follower_failure_is_not_hidden(error: str) -> None:
    client = FollowerClient(error=error)
    with pytest.raises((RuntimeError, TimeoutError, pytest.fail.Exception)):
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", _charm())
    if error != "validation":
        assert not any(call[0] == "validate_model" for call in client.calls)


def test_follower_requires_ha_count_before_disruption() -> None:
    client = FollowerClient(current_units=2)
    with pytest.raises(pytest.fail.Exception, match="at least 3"):
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", _charm())
    assert not any(call[0] == "restart" for call in client.calls)


@pytest.mark.parametrize("reason", ["single", "subordinate", "unsupported"])
def test_follower_skips_unsupported_configuration(reason: str) -> None:
    client = FollowerClient(error=reason, current_units=1 if reason == "single" else 3)
    charm = _charm(ha_units=1 if reason == "single" else 3, subordinate=reason == "subordinate")
    with pytest.raises(pytest.skip.Exception):
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", charm)
    assert not any(call[0] == "validate_model" for call in client.calls)


def test_follower_does_not_skip_unexpected_not_implemented_error() -> None:
    client = FollowerClient(error="not-implemented")
    with pytest.raises(NotImplementedError, match="restart hook failed"):
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", _charm())
    assert not any(call[0] == "validate_model" for call in client.calls)
