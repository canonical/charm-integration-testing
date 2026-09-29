# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""TEMPORARY SQT-905: remove with the kernel diagnostic."""

from pathlib import Path

import pytest
from test_suite.temporary_cpu_snapshot import cgroup_path, snapshot


def write(root: Path, name: str, value: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    proc = tmp_path / "proc"
    group = tmp_path / "cgroup"
    group.mkdir()
    write(proc, "self/cgroup", "0::/\n")
    write(proc, "self/mountinfo", f"30 20 0:25 / {group} ro - cgroup2 cgroup rw\n")
    write(group, "cpu.stat", "usage_usec 9000000\nnr_periods 100\nnr_throttled 80\nthrottled_usec 500000\n")
    write(group, "cpu.max", "100000 100000")
    write(group, "cpu.pressure", "some avg10=30.00 avg60=20.00 avg300=10.00 total=500000\n")
    fields = ["S"] + ["0"] * 18 + ["123"]
    write(proc, "1/stat", "1 (init) " + " ".join(fields))
    write(proc, "42/comm", "stress-ng-cpu")
    fields[11:13] = ["50", "10"]
    write(proc, "42/stat", "42 (stress-ng-cpu) " + " ".join(fields))
    write(proc, "42/cgroup", "0::/\n")
    write(proc, "42/schedstat", "100000 200000 10")
    # Process arguments/environment must never be returned.
    write(proc, "42/cmdline", "secret-password")
    write(proc, "42/environ", "secret-token")
    return proc


def test_collects_target_quota_pressure_and_worker_identity(proc: Path) -> None:
    result = snapshot(proc)
    assert result["available"] is True
    assert result["cpu_max"] == "100000 100000"
    assert result["cpu_stat"]["nr_throttled"] == 80
    assert result["cpu_pressure"]["some"]["total"] == 500000
    worker = result["workers"][0]
    assert worker["state"] == "S"
    assert worker["start_ticks"] == 123
    assert worker["user_ticks"] == 50
    assert worker["system_ticks"] == 10
    assert worker["same_cgroup"] is True
    assert "secret" not in str(result)


def test_missing_worker_membership_is_unknown(proc: Path) -> None:
    (proc / "42/cgroup").unlink()
    assert snapshot(proc)["workers"][0]["same_cgroup"] is None


def test_unavailable_quota_is_not_reported_as_zero_load(proc: Path) -> None:
    group = cgroup_path(proc)
    assert group is not None
    (group / "cpu.stat").unlink()
    assert snapshot(proc)["available"] is False


def test_resolves_non_root_cgroup_mount_and_rejects_v1(proc: Path, tmp_path: Path) -> None:
    write(proc, "self/cgroup", "0::/pods/target\n")
    write(proc, "self/mountinfo", f"30 20 0:25 /pods {tmp_path} ro - cgroup2 cgroup rw\n")
    assert cgroup_path(proc) == tmp_path / "target"
    write(proc, "self/cgroup", "2:cpu:/pods/target\n")
    assert snapshot(proc)["available"] is False
