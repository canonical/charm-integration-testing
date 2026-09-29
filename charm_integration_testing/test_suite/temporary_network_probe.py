# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""TEMPORARY SQT-909: remove this helper and its tests after live diagnosis."""

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

LOGGER = logging.getLogger(__name__)
TCP_PROBE = """
import json, socket, sys, time
started = time.monotonic()
try:
    with socket.create_connection((sys.argv[1], 5432), timeout=3):
        result = {'connected': True}
except OSError as error:
    result = {'connected': False, 'error': type(error).__name__, 'errno': error.errno}
result['elapsed_seconds'] = round(time.monotonic() - started, 3)
print('SQT909_TCP=' + json.dumps(result))
"""


class NetworkIsolationProbe:
    """Record same-source, same-destination TCP attempts without changing the verdict."""

    def __init__(self, backend: KubernetesBackend, kubeconfig: Path | None, namespace: str, unit: str) -> None:
        self.backend = backend
        self.kubeconfig = kubeconfig
        self.namespace = namespace
        self.unit = unit
        self.name = f"cit-network-probe-{uuid4().hex[:16]}"
        self.uid: str | None = None
        self.attempted = False
        self.ready = False
        self.target: tuple[str, str, str] | None = None

    def prepare(self) -> None:
        try:
            if self.kubeconfig is None:
                raise ValueError("No target kubeconfig")
            self.attempted = True
            created = self.backend.core_v1_api.create_namespaced_pod(
                namespace=self.namespace,
                body={
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {"name": self.name, "labels": {"cit-diagnostic": "sqt-909"}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "restartPolicy": "Never",
                        "activeDeadlineSeconds": 1800,
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532},
                        "containers": [
                            {
                                "name": "probe",
                                "image": "docker.io/library/python:3.12-alpine",
                                "command": ["python", "-c", "import time; time.sleep(1800)"],
                                "resources": {
                                    "requests": {"cpu": "10m", "memory": "16Mi"},
                                    "limits": {"cpu": "100m", "memory": "64Mi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                            }
                        ],
                    },
                },
                _request_timeout=5,
            )
            self.uid = created.metadata.uid
            if not self.uid:
                raise RuntimeError("Creation UID missing")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                pod = self.backend.core_v1_api.read_namespaced_pod(self.name, self.namespace, _request_timeout=5)
                if pod.metadata.uid != self.uid:
                    raise RuntimeError("Probe Pod replaced")
                if any(c.type == "Ready" and c.status == "True" for c in pod.status.conditions or []):
                    self.ready = True
                    return
                if pod.status.phase in {"Failed", "Succeeded"}:
                    raise RuntimeError("Probe Pod stopped")
                time.sleep(2)
            raise TimeoutError("Probe Pod not ready")
        except Exception as error:
            LOGGER.warning("SQT-909 PROBE setup unavailable (%s); connectivity is inconclusive", type(error).__name__)

    def _target(self) -> tuple[str, str, str]:
        pods = self.backend.core_v1_api.list_namespaced_pod(namespace=self.namespace, _request_timeout=5)
        targets = [
            pod
            for pod in pods.items
            if (pod.metadata.annotations or {}).get("unit.juju.is/id") == self.unit
            and pod.metadata.deletion_timestamp is None
            and pod.status.phase not in {"Failed", "Succeeded"}
        ]
        if len(targets) != 1:
            raise RuntimeError("Expected one target Pod")
        pod = targets[0]
        LOGGER.info(
            "SQT-909 PROBE target: namespace=%s name=%s uid=%s ip=%s labels=%s node=%s",
            self.namespace,
            pod.metadata.name,
            pod.metadata.uid,
            pod.status.pod_ip,
            pod.metadata.labels,
            pod.spec.node_name,
        )
        if not pod.status.pod_ip or not pod.metadata.uid:
            raise RuntimeError("Target IP or UID missing")
        return pod.metadata.name, pod.metadata.uid, pod.status.pod_ip

    def _connect(self) -> dict[str, Any]:
        current = self._target()
        if self.target is None:
            self.target = current
        if self.target != current:
            raise RuntimeError("Target identity/IP changed; comparison is inconclusive")
        if not self.ready or self.kubeconfig is None:
            raise RuntimeError("Probe unavailable")
        source = self.backend.core_v1_api.read_namespaced_pod(self.name, self.namespace, _request_timeout=5)
        if source.metadata.uid != self.uid:
            raise RuntimeError("Probe Pod replaced")
        LOGGER.info(
            "SQT-909 PROBE source: name=%s uid=%s ip=%s labels=%s node=%s destination=%s:5432",
            self.name,
            self.uid,
            source.status.pod_ip,
            source.metadata.labels,
            source.spec.node_name,
            current[2],
        )
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(self.kubeconfig),
                "--request-timeout=5s",
                "-n",
                self.namespace,
                "exec",
                self.name,
                "-c",
                "probe",
                "--",
                "python",
                "-c",
                TCP_PROBE,
                current[2],
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Probe exec failed; not evidence of TCP blocking")
        for line in result.stdout.splitlines():
            if line.startswith("SQT909_TCP="):
                value: dict[str, Any] = json.loads(line.removeprefix("SQT909_TCP="))
                return value
        raise RuntimeError("Probe result missing")

    def sample(self, phase: str) -> None:
        LOGGER.info("SQT-909 PROBE [%s] namespace=%s", phase, self.namespace)
        try:
            policies = self.backend.networking_v1_api.list_namespaced_network_policy(
                namespace=self.namespace, _request_timeout=5
            )
            LOGGER.info(
                "SQT-909 PROBE [%s] namespace policies=%s",
                phase,
                [{"name": p.metadata.name, "uid": p.metadata.uid, "spec": p.spec.to_dict()} for p in policies.items],
            )
        except Exception as error:
            LOGGER.warning("SQT-909 PROBE [%s] policies unavailable (%s)", phase, type(error).__name__)
        try:
            LOGGER.info("SQT-909 PROBE [%s] TCP result=%s", phase, self._connect())
        except Exception as error:
            LOGGER.warning("SQT-909 PROBE [%s] TCP inconclusive (%s)", phase, type(error).__name__)

    def after_removal(self, succeeded: bool) -> None:
        phase = "after policy removal" if succeeded else "after removal failure"
        self.sample(phase)
        # Policy enforcement can lag behind the API response.
        time.sleep(5)
        self.sample(phase + " +5s")

    def cleanup(self) -> None:
        if not self.attempted:
            return
        try:
            if not self.uid:
                # A lost creation response is not proof of the current Pod's identity.
                raise RuntimeError(f"Cannot safely delete diagnostic Pod {self.namespace}/{self.name}: UID unknown")
            self.backend.core_v1_api.delete_namespaced_pod(
                name=self.name,
                namespace=self.namespace,
                body={"preconditions": {"uid": self.uid}, "gracePeriodSeconds": 0},
                _request_timeout=5,
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current = self.backend.core_v1_api.read_namespaced_pod(self.name, self.namespace, _request_timeout=5)
                if current.metadata.uid != self.uid:
                    break  # The original is gone; never touch a replacement.
                time.sleep(1)
            else:
                raise TimeoutError(f"Diagnostic Pod {self.namespace}/{self.name} was not removed")
        except ApiException as error:
            if error.status != 404:
                raise
        self.attempted = False
