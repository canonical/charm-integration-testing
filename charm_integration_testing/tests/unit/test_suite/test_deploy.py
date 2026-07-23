# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

from test_suite.test_deploy import (
    _DEFAULT_IDLE_TIMEOUT,
    _EXTENDED_IDLE_TIMEOUT,
    _idle_timeout_for_bundle,
)

# ---------------------------------------------------------------------------
# Tests: _idle_timeout_for_bundle
# ---------------------------------------------------------------------------


def test_idle_timeout_default_for_unrelated_bundle(tmp_path: Path) -> None:
    # GIVEN a bundle with no slow-settling charms
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text("applications:\n" "  glauth-k8s:\n" "    charm: glauth-k8s\n" "    channel: latest/edge\n")
    # WHEN computing the idle timeout
    result = _idle_timeout_for_bundle(bundle)
    # THEN the default timeout is used
    assert result == _DEFAULT_IDLE_TIMEOUT


def test_idle_timeout_extended_for_postgresql_k8s(tmp_path: Path) -> None:
    # GIVEN a bundle containing postgresql-k8s and pgbouncer-k8s
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        "applications:\n"
        "  postgresql-k8s:\n"
        "    charm: postgresql-k8s\n"
        "    channel: 14/stable\n"
        "  pgbouncer-k8s:\n"
        "    charm: pgbouncer-k8s\n"
        "    channel: 1/stable\n"
    )
    # WHEN computing the idle timeout
    result = _idle_timeout_for_bundle(bundle)
    # THEN the extended timeout is used
    assert result == _EXTENDED_IDLE_TIMEOUT


def test_idle_timeout_extended_for_machine_postgresql(tmp_path: Path) -> None:
    # GIVEN a bundle containing the machine postgresql charm
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text("applications:\n  postgresql:\n    charm: postgresql\n    channel: 14/stable\n")
    # WHEN computing the idle timeout
    result = _idle_timeout_for_bundle(bundle)
    # THEN the extended timeout is used
    assert result == _EXTENDED_IDLE_TIMEOUT


def test_idle_timeout_extended_timeout_is_longer_than_default() -> None:
    assert _EXTENDED_IDLE_TIMEOUT > _DEFAULT_IDLE_TIMEOUT
    assert isinstance(_DEFAULT_IDLE_TIMEOUT, timedelta)
    assert isinstance(_EXTENDED_IDLE_TIMEOUT, timedelta)
