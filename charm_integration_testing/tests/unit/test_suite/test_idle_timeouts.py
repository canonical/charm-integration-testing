# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

from test_suite._idle_timeouts import (
    DEFAULT_IDLE_TIMEOUT,
    EXTENDED_IDLE_TIMEOUT,
    idle_timeout_for_bundle,
    idle_timeout_for_bundles,
)

# ---------------------------------------------------------------------------
# Tests: idle_timeout_for_bundle
# ---------------------------------------------------------------------------


def test_idle_timeout_default_for_unrelated_bundle(tmp_path: Path) -> None:
    # GIVEN a bundle with no slow-settling charms
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text("applications:\n" "  glauth-k8s:\n" "    charm: glauth-k8s\n" "    channel: latest/edge\n")
    # WHEN computing the idle timeout
    result = idle_timeout_for_bundle(bundle)
    # THEN the default timeout is used
    assert result == DEFAULT_IDLE_TIMEOUT


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
    result = idle_timeout_for_bundle(bundle)
    # THEN the extended timeout is used
    assert result == EXTENDED_IDLE_TIMEOUT


def test_idle_timeout_extended_for_machine_postgresql(tmp_path: Path) -> None:
    # GIVEN a bundle containing the machine postgresql charm
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text("applications:\n  postgresql:\n    charm: postgresql\n    channel: 14/stable\n")
    # WHEN computing the idle timeout
    result = idle_timeout_for_bundle(bundle)
    # THEN the extended timeout is used
    assert result == EXTENDED_IDLE_TIMEOUT


def test_idle_timeout_extended_timeout_is_longer_than_default() -> None:
    assert EXTENDED_IDLE_TIMEOUT > DEFAULT_IDLE_TIMEOUT
    assert isinstance(DEFAULT_IDLE_TIMEOUT, timedelta)
    assert isinstance(EXTENDED_IDLE_TIMEOUT, timedelta)


# ---------------------------------------------------------------------------
# Tests: idle_timeout_for_bundles
# ---------------------------------------------------------------------------


def test_idle_timeout_for_bundles_default_when_none_slow(tmp_path: Path) -> None:
    # GIVEN two bundles with no slow-settling charms
    bundle_a = tmp_path / "a.yaml"
    bundle_a.write_text("applications:\n  glauth-k8s:\n    charm: glauth-k8s\n    channel: latest/edge\n")
    bundle_b = tmp_path / "b.yaml"
    bundle_b.write_text("applications:\n  traefik-k8s:\n    charm: traefik-k8s\n    channel: latest/edge\n")
    # WHEN computing the idle timeout across both
    result = idle_timeout_for_bundles([bundle_a, bundle_b])
    # THEN the default timeout is used
    assert result == DEFAULT_IDLE_TIMEOUT


def test_idle_timeout_for_bundles_extended_when_any_bundle_is_slow(tmp_path: Path) -> None:
    # GIVEN one bundle with no slow-settling charms and another with postgresql-k8s
    bundle_a = tmp_path / "a.yaml"
    bundle_a.write_text("applications:\n  glauth-k8s:\n    charm: glauth-k8s\n    channel: latest/edge\n")
    bundle_b = tmp_path / "b.yaml"
    bundle_b.write_text("applications:\n  postgresql-k8s:\n    charm: postgresql-k8s\n    channel: 14/stable\n")
    # WHEN computing the idle timeout across both
    result = idle_timeout_for_bundles([bundle_a, bundle_b])
    # THEN the extended timeout is used
    assert result == EXTENDED_IDLE_TIMEOUT


def test_idle_timeout_for_bundles_ignores_none_entries(tmp_path: Path) -> None:
    # GIVEN a single non-slow bundle and a missing (None) neighbor bundle
    bundle = tmp_path / "a.yaml"
    bundle.write_text("applications:\n  glauth-k8s:\n    charm: glauth-k8s\n    channel: latest/edge\n")
    # WHEN computing the idle timeout, tolerating the None entry
    result = idle_timeout_for_bundles([bundle, None])
    # THEN the default timeout is used
    assert result == DEFAULT_IDLE_TIMEOUT
