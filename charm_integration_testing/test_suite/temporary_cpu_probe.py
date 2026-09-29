# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""TEMPORARY SQT-905: remove this observer and its tests after live diagnosis."""

import json
import logging
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable, Iterator

LOGGER = logging.getLogger(__name__)


class CpuStressProbe:
    """Observe without changing test verdicts; use bounded, separate CLI processes.

    SQL probing is specific to postgresql-k8s revision 495: its local peer mapping
    permits OS user postgres to connect as DB user backup. No passwords are read.
    This measures a local query, not connectivity from a remote application.
    """

    def __init__(
        self, kubeconfig: Path | None, model: str, namespace: str, pod: str, unit: str, container: str
    ) -> None:
        self.kubeconfig = kubeconfig
        self.model = model
        self.namespace = namespace
        self.pod = pod
        self.unit = unit
        self.container = container
        self.stop = Event()

    def _run(self, args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, input=input_text, text=True, capture_output=True, timeout=8, check=False)

    def _kubectl(self, args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        if self.kubeconfig is None:
            raise ValueError("No target kubeconfig; do not fall back to another cluster")
        return self._run(
            ["kubectl", "--kubeconfig", str(self.kubeconfig), "--request-timeout=5s", "-n", self.namespace, *args],
            input_text,
        )

    def _sql(self) -> dict[str, Any]:
        if self.container != "postgresql":
            return {"result": "unsupported container; PostgreSQL-only diagnostic"}
        started = time.monotonic()
        result = self._kubectl(
            [
                "exec",
                "-i",
                self.pod,
                "-c",
                self.container,
                "--",
                "timeout",
                "6s",
                "runuser",
                "-u",
                "postgres",
                "--",
                "env",
                "LC_ALL=C",
                "PGCONNECT_TIMEOUT=3",
                "PGOPTIONS=-c statement_timeout=3000",
                "psql",
                "-X",
                "-w",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-h",
                "/var/run/postgresql",
                "-U",
                "backup",
                "-d",
                "postgres",
            ],
            "\\timing on\nSELECT 1;\n",
        )
        lines = result.stdout.splitlines()
        return {
            "query_ok": result.returncode == 0 and "1" in lines,
            "exit_code": result.returncode,
            "sql_timing": [line for line in lines if line.startswith("Time:")],
            "exec_elapsed_seconds": round(time.monotonic() - started, 3),
            "scope": "local socket; exec time includes Kubernetes transport and process startup",
            "failure_note": "Nonzero exit may mean probe/auth/tool failure, not necessarily DB failure",
        }

    def _metrics(self) -> dict[str, Any]:
        result = self._kubectl(
            ["get", "--raw", f"/apis/metrics.k8s.io/v1beta1/namespaces/{self.namespace}/pods/{self.pod}"]
        )
        if result.returncode:
            raise RuntimeError("Metrics unavailable")
        metrics = json.loads(result.stdout)
        return {key: metrics.get(key) for key in ("timestamp", "window", "containers")}

    def _status(self) -> dict[str, Any]:
        result = self._run(["juju", "status", "-m", self.model, "--format=json"])
        if result.returncode:
            raise RuntimeError("Juju status unavailable")
        status = json.loads(result.stdout)
        unit = status.get("applications", {}).get(self.unit.split("/")[0], {}).get("units", {}).get(self.unit)
        if unit is None:
            return {"unit": self.unit, "present": False}
        return {"unit": self.unit, "workload": unit.get("workload-status"), "agent": unit.get("juju-status")}

    def sample(self, phase: str) -> None:
        readers: list[tuple[str, Callable[[], Any]]] = [
            ("SQL", self._sql),
            ("CPU", self._metrics),
            ("Juju", self._status),
        ]
        for label, read in readers:
            timestamp = datetime.now(timezone.utc).isoformat()
            try:
                LOGGER.info("SQT-905 PROBE [%s] %s %s: %s", phase, timestamp, label, read())
            except Exception as error:
                # Do not print raw command output/errors: they may contain credentials.
                LOGGER.warning(
                    "SQT-905 PROBE [%s] %s %s unavailable (%s)", phase, timestamp, label, type(error).__name__
                )

    @contextmanager
    def during_stress(self) -> Iterator[None]:
        def observe() -> None:
            while not self.stop.wait(10):
                self.sample("during stress attempt")

        worker = Thread(target=observe, name="temporary-sqt905-probe", daemon=True)
        started = False
        try:
            try:
                worker.start()
                started = True
            except RuntimeError:
                LOGGER.warning("SQT-905 PROBE periodic observer could not start")
            yield
        finally:
            self.stop.set()
            if started:
                # Three sequential commands, each bounded at eight seconds.
                worker.join(timeout=30)
                if worker.is_alive():
                    LOGGER.warning("SQT-905 PROBE observer did not stop within its expected bound")

    def after_cleanup(self, succeeded: bool) -> None:
        phase = "after cleanup" if succeeded else "after cleanup failure (stress may remain)"
        try:
            self.sample(phase)
            time.sleep(10)
            self.sample(phase + " +10s")
        except Exception as error:
            LOGGER.warning("SQT-905 PROBE post-cleanup observation unavailable (%s)", type(error).__name__)
