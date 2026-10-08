# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from juju import JujuClient, JujuModelHandle, JujuRestartNotSupportedError
from test_suite import test_scale_ha as scale_ha
from test_suite import test_scale_remove_follower as remove_follower
from test_suite.fixtures import integration_spec

from .ha_fakes import MODEL, RecordingJujuClient, charm


def _write_bundle(tmp_path: Path, *, application: str = "target", units: int = 2) -> Path:
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(f"applications:\n  {application}:\n    scale: {units}\n", encoding="utf-8")
    return bundle


def test_scale_to_ha_validates_immediately_when_application_is_already_large_enough() -> None:
    client = RecordingJujuClient(current_units=4)

    scale_ha.test_scale_to_ha(cast(JujuClient, client), MODEL, "target", charm())

    assert client.calls == [
        ("num_units", "target", MODEL),
        ("validate_model", MODEL, "deep"),
    ]


def test_scale_to_ha_scales_waits_and_deep_validates() -> None:
    client = RecordingJujuClient(current_units=1)

    scale_ha.test_scale_to_ha(cast(JujuClient, client), MODEL, "target", charm(ha_units=5))

    assert client.calls == [
        ("num_units", "target", MODEL),
        ("scale_application", "target", 5, MODEL),
        ("idle_for_period", MODEL, timedelta(minutes=15)),
        ("validate_model", MODEL, "deep"),
    ]


def test_scale_to_ha_skips_subordinate_before_accessing_juju() -> None:
    client = RecordingJujuClient(current_units=1)

    with pytest.raises(pytest.skip.Exception, match="mysql-k8s is subordinate"):
        scale_ha.test_scale_to_ha(cast(JujuClient, client), MODEL, "target", charm(subordinate=True))

    assert client.calls == []


def test_scale_from_ha_restores_original_units_waits_and_simple_validates(tmp_path: Path) -> None:
    client = RecordingJujuClient(current_units=3)

    scale_ha.test_scale_from_ha(
        cast(JujuClient, client),
        MODEL,
        _write_bundle(tmp_path),
        "target",
        "kubernetes",
        charm(),
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
            charm(scale_down=False),
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
            charm(subordinate=True),
        )

    assert client.calls == []


def test_scale_to_ha_fails_if_deployed_charm_metadata_is_unavailable() -> None:
    client = RecordingJujuClient(current_units=1)

    with pytest.raises(pytest.fail.Exception, match="Unable to resolve the deployed target charm metadata"):
        scale_ha.test_scale_to_ha(cast(JujuClient, client), MODEL, "target", None)

    assert client.calls == []


def test_require_principal_charm_rejects_subordinates() -> None:
    with pytest.raises(pytest.skip.Exception, match="mysql-k8s is subordinate"):
        integration_spec.require_principal_charm(charm(subordinate=True))


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
            self.units.remove("target/0")
        return "target/1"

    def validate_model(self, model: JujuModelHandle, level: str = "simple") -> None:
        super().validate_model(model, level)
        if self.error == "validation":
            raise RuntimeError("validation")


def test_follower_restart_preserves_ha_without_requiring_scale_down() -> None:
    client = FollowerClient()
    neighbor = JujuModelHandle(controller="controller", model="neighbor")
    remove_follower.test_scale_remove_follower(
        cast(JujuClient, client), MODEL, neighbor, "target", charm(scale_down=False)
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
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", charm())
    if error != "validation":
        assert not any(call[0] == "validate_model" for call in client.calls)


def test_follower_requires_ha_count_before_disruption() -> None:
    client = FollowerClient(current_units=2)
    with pytest.raises(pytest.fail.Exception, match="at least 3"):
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", charm())
    assert not any(call[0] == "restart" for call in client.calls)


@pytest.mark.parametrize("reason", ["single", "subordinate", "unsupported"])
def test_follower_skips_unsupported_configuration(reason: str) -> None:
    client = FollowerClient(error=reason, current_units=1 if reason == "single" else 3)
    deployed_charm = charm(ha_units=1 if reason == "single" else 3, subordinate=reason == "subordinate")
    with pytest.raises(pytest.skip.Exception):
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", deployed_charm)
    assert not any(call[0] == "validate_model" for call in client.calls)


def test_follower_does_not_skip_unexpected_not_implemented_error() -> None:
    client = FollowerClient(error="not-implemented")
    with pytest.raises(NotImplementedError, match="restart hook failed"):
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", charm())
    assert not any(call[0] == "validate_model" for call in client.calls)
