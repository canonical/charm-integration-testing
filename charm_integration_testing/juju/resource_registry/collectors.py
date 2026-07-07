# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess  # nosec B404
from pathlib import Path

from resource_registry.protocols import ResourceHandle

from ..backend import JujuBackend
from .handles import JujuControllerHandle

_JUJU_CRASHDUMP_TIMEOUT_SECONDS = 300
_JUJU_CRASHDUMP_MAX_FILE_SIZE_BYTES = 100_000_000
# Subprocess timeout is double the internal tool timeout to allow for startup and
# file compression overhead.
_SUBPROCESS_TIMEOUT_SECONDS = _JUJU_CRASHDUMP_TIMEOUT_SECONDS * 2


class JujuCrashdumpCollector:
    """Collect controller logs using juju-crashdump or juju-k8s-crashdump.

    Cloud type is determined per-controller by querying the backend, which selects
    the appropriate log collection tool independently for each controller.
    """

    def __init__(
        self,
        logger: logging.Logger,
        backend: JujuBackend,
        output_dir: Path | None = None,
        kubeconfig_path: Path | None = None,
    ) -> None:
        self._logger = logger.getChild(type(self).__name__)
        self._backend = backend
        self._output_dir = output_dir
        self._kubeconfig_path = kubeconfig_path

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

        if self._backend.is_k8s_model(f"{handle.controller}:controller"):
            if self._kubeconfig_path is None:
                raise ValueError(f"Controller '{handle.controller}' is K8s-based but kubeconfig_path not available")
            self._collect_k8s(handle.controller, output_dir / f"{handle.path_segment}.tar.gz")
        else:
            self._collect_machine(handle.controller, output_dir / f"{handle.path_segment}.tar.gz")

    def _collect_k8s(self, controller: str, output_path: Path) -> None:
        if self._kubeconfig_path is None:
            raise ValueError("kubeconfig_path is required for K8s log collection")
        cmd = [
            "juju-k8s-crashdump",
            str(self._kubeconfig_path.resolve()),
            controller,
            "--output_path",
            str(output_path),
        ]
        self._logger.debug(f"Running {' '.join(str(c) for c in cmd)}")
        result = subprocess.run(  # nosec B603
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
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
            "--unit-dump-location",
            str(output_path),
            "--as-root",
        ]
        self._logger.debug(f"Running {' '.join(str(c) for c in cmd)}")
        result = subprocess.run(  # nosec B603
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
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
        # Verify the expected output file was created
        if not output_path.exists():
            raise FileNotFoundError(f"juju-crashdump succeeded but expected output file not found: {output_path}")
