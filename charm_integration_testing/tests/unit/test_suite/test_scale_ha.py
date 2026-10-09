# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from juju import JujuClient
from test_suite import test_scale_ha as scale_ha
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
