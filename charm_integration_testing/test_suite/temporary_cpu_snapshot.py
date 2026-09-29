# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""TEMPORARY SQT-905: read-only script sent to the workload container; remove after diagnosis.

Only the standard library is used. Never read process arguments or environments.
Counters are cumulative; the observer computes differences between samples.
"""

import json
import time
from pathlib import Path
from typing import Any


def read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def counters(value: str | None) -> dict[str, int]:
    return {key: int(number) for key, number in (line.split() for line in (value or "").splitlines())}


def pressure(value: str | None) -> dict[str, dict[str, float]]:
    return {
        parts[0]: {key: float(number) for key, number in (item.split("=") for item in parts[1:])}
        for parts in (line.split() for line in (value or "").splitlines())
    }


def cgroup_path(proc: Path) -> Path | None:
    """Resolve the process's v2 cgroup within its visible cgroup mount."""
    group = next((line[3:] for line in (read(proc / "self/cgroup") or "").splitlines() if line.startswith("0::")), None)
    if group is None:
        return None
    for line in (read(proc / "self/mountinfo") or "").splitlines():
        fields = line.split()
        if fields[fields.index("-") + 1] != "cgroup2":
            continue
        root, mount = (item.replace("\\040", " ").replace("\\134", "\\") for item in fields[3:5])
        if group == "/":  # A private cgroup namespace exposes this container as its root.
            return Path(mount)
        try:
            return Path(mount) / Path(group).relative_to(root)
        except ValueError:
            continue
    return None


def snapshot(proc: Path = Path("/proc")) -> dict[str, Any]:
    group = cgroup_path(proc)
    if group is None:
        return {"available": False, "reason": "No resolvable cgroup v2 mount"}
    stat = read(group / "cpu.stat")
    if stat is None:
        return {"available": False, "reason": "Cannot read target cpu.stat"}
    init_stat = read(proc / "1/stat")
    identity = [
        read(proc / "sys/kernel/random/boot_id"),
        init_stat.rsplit(")", 1)[1].split()[19] if init_stat else None,
        str(group),
        group.stat().st_ino,
    ]
    target_cgroup = read(proc / "self/cgroup")
    workers = []
    unavailable = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        name = read(entry / "comm")
        if name is None:
            unavailable += 1
            continue
        if not name.startswith("stress-ng"):
            continue
        process_stat = read(entry / "stat")
        if process_stat is None:
            unavailable += 1
            continue
        fields = process_stat.rsplit(")", 1)[1].split()
        worker_cgroup = read(entry / "cgroup")
        workers.append(
            {
                "pid": int(entry.name),
                "name": name,
                "state": fields[0],
                "start_ticks": int(fields[19]),
                "user_ticks": int(fields[11]),
                "system_ticks": int(fields[12]),
                "schedstat": read(entry / "schedstat"),
                "cgroup": worker_cgroup,
                "same_cgroup": worker_cgroup == target_cgroup if worker_cgroup is not None else None,
            }
        )
        if len(workers) >= 64:
            break
    return {
        "available": True,
        "identity": identity,
        "monotonic_seconds": time.monotonic(),
        "cpu_stat": counters(stat),
        "cpu_pressure": pressure(read(group / "cpu.pressure")),
        "cpu_max": read(group / "cpu.max"),
        "cpu_weight": read(group / "cpu.weight"),
        "cpuset_effective": read(group / "cpuset.cpus.effective"),
        "cgroup": target_cgroup,
        "workers": workers,
        "worker_list_capped": len(workers) >= 64,
        "unreadable_or_exited_processes": unavailable,
        "worker_scope": "Visible PID namespace only; empty list does not prove absence of stress",
        "visible_system_cpu": (read(proc / "stat") or "").splitlines()[:1],
        "visible_system_loadavg": read(proc / "loadavg"),
        "visible_system_cpu_pressure": pressure(read(proc / "pressure/cpu")),
        "system_scope": "Procfs view, not necessarily the container; ancestors outside the mount are not measured",
    }


if __name__ == "__main__":
    print(json.dumps(snapshot()))
