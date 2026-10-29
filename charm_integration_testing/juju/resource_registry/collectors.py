# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from __future__ import annotations

import logging
import subprocess  # nosec B404
from pathlib import Path
from typing import TYPE_CHECKING

from kubernetes_client import KubernetesClientRegistry
from resource_registry.protocols import ResourceHandle
from utils.timeouts import remaining_timeout

from ..handles import JujuControllerHandle, JujuModelHandle

if TYPE_CHECKING:
    from juju.backend import JujuBackend

_JUJU_CRASHDUMP_TIMEOUT_SECONDS = 300
_JUJU_CRASHDUMP_MAX_FILE_SIZE_BYTES = 100_000_000
# Subprocess timeout is double the internal tool timeout to allow for startup and
# file compression overhead.
_SUBPROCESS_TIMEOUT_SECONDS = _JUJU_CRASHDUMP_TIMEOUT_SECONDS * 2


class JujuCrashdumpCollector:
    """Collect controller logs using juju-crashdump or juju-k8s-crashdump.

    Juju supplies cloud metadata; KubernetesClientRegistry owns kubeconfig configuration.
    """

    def __init__(
        self,
        logger: logging.Logger,
        backend: JujuBackend,
        output_dir: Path | None = None,
        kubernetes_client_registry: KubernetesClientRegistry | None = None,
    ) -> None:
        self._logger = logger.getChild(type(self).__name__)
        self._backend = backend
        self._kubernetes_client_registry = kubernetes_client_registry
        self._output_dir = output_dir

    def supports(self, handle: ResourceHandle) -> bool:
        return isinstance(handle, JujuControllerHandle)

    def collect(self, handle: ResourceHandle) -> None:
        if not isinstance(handle, JujuControllerHandle):
            return
        if self._output_dir is None:
            self._logger.debug(f"Output_dir is None, skipping log collection for '{handle.controller}'")
            return

        output_dir = self._output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{handle.path_segment}.tar.gz"

        controller_model = JujuModelHandle(controller=handle.controller, model="controller")
        cloud = self._backend.kubernetes_cloud_for_model(controller_model)
        if cloud is not None:
            if self._kubernetes_client_registry is None:
                raise RuntimeError("KubernetesClientRegistry is required to collect Kubernetes controller logs.")
            kubeconfig = self._kubernetes_client_registry.kubeconfig_for_cloud(cloud)
            self._collect_k8s(handle.controller, output_path, kubeconfig)
        else:
            self._collect_machine(handle.controller, output_path)

    def _collect_k8s(self, controller: str, output_path: Path, kubeconfig: Path) -> None:
        cmd = [
            "juju-k8s-crashdump",
            str(kubeconfig.resolve()),
            controller,
            "--output_path",
            str(output_path),
        ]
        self._logger.debug(f"Running {' '.join(str(c) for c in cmd)}")
        result = subprocess.run(  # nosec B603
            cmd,
            capture_output=True,
            text=True,
            timeout=remaining_timeout(_SUBPROCESS_TIMEOUT_SECONDS),
        )
        if result.returncode != 0:
            if result.stdout:
                self._logger.warning(f"juju-k8s-crashdump stdout:\n{result.stdout}")
            if result.stderr:
                self._logger.warning(f"juju-k8s-crashdump stderr:\n{result.stderr}")
        else:
            if result.stdout:
                self._logger.debug(f"juju-k8s-crashdump stdout:\n{result.stdout}")
            if result.stderr:
                self._logger.debug(f"juju-k8s-crashdump stderr:\n{result.stderr}")
        result.check_returncode()

    def _collect_machine(self, controller: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "juju-crashdump",
            "--model",
            f"{controller}:controller",
            "--timeout",
            str(_JUJU_CRASHDUMP_TIMEOUT_SECONDS),
            "--small",
            "-f",
            str(_JUJU_CRASHDUMP_MAX_FILE_SIZE_BYTES),
            "--compression",
            "gz",
            "-o",
            str(output_path.parent),
            "-u",
            controller,
            "--as-root",
        ]
        self._logger.debug(f"Running {' '.join(str(c) for c in cmd)}")
        result = subprocess.run(  # nosec B603
            cmd,
            capture_output=True,
            text=True,
            timeout=remaining_timeout(_SUBPROCESS_TIMEOUT_SECONDS),
        )
        if result.returncode != 0:
            if result.stdout:
                self._logger.warning(f"juju-crashdump stdout:\n{result.stdout}")
            if result.stderr:
                self._logger.warning(f"juju-crashdump stderr:\n{result.stderr}")
        else:
            if result.stdout:
                self._logger.debug(f"juju-crashdump stdout:\n{result.stdout}")
            if result.stderr:
                self._logger.debug(f"juju-crashdump stderr:\n{result.stderr}")
        result.check_returncode()
        # juju-crashdump writes to <output_dir>/juju-crashdump-<controller>.tar.gz;
        # rename to the caller's expected output_path.
        generated = output_path.parent / f"juju-crashdump-{controller}.tar.gz"
        if not generated.exists():
            raise FileNotFoundError(f"juju-crashdump succeeded but expected output file not found: {generated}")
        generated.replace(output_path)
