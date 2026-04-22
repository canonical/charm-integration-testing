# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess  # nosec B404
from pathlib import Path

from resource_registry.protocols import ResourceHandle

from .handles import JujuControllerHandle

_JUJU_CRASHDUMP_TIMEOUT_SECONDS = 300
_JUJU_CRASHDUMP_MAX_FILE_SIZE_BYTES = 100_000_000
# Subprocess timeout is double the internal tool timeout to allow for startup and
# file compression overhead.
_SUBPROCESS_TIMEOUT_SECONDS = _JUJU_CRASHDUMP_TIMEOUT_SECONDS * 2


class JujuCrashdumpCollector:
    """Collect controller logs using juju-crashdump or juju-k8s-crashdump.

    For Kubernetes substrates, set kubeconfig_path to the path of the kubeconfig
    file; juju-k8s-crashdump will be used.  For machine/OpenStack substrates leave
    kubeconfig_path as None; juju-crashdump will be used instead.
    """

    def __init__(
        self,
        logger: logging.Logger,
        output_dir: Path | None = None,
        kubeconfig_path: str | None = None,
    ) -> None:
        self._logger = logger.getChild(type(self).__name__)
        self._output_dir = output_dir
        self._kubeconfig_path = kubeconfig_path

    def supports(self, handle: ResourceHandle) -> bool:
        return isinstance(handle, JujuControllerHandle)

    def collect(self, handle: ResourceHandle) -> None:
        if not isinstance(handle, JujuControllerHandle):
            return
        if self._output_dir is None:
            self._logger.debug(f"output_dir is None, skipping log collection for '{handle.controller}'")
            return

        dest = self._output_dir / handle.path_segment
        dest.mkdir(parents=True, exist_ok=True)

        if self._kubeconfig_path is not None:
            self._collect_k8s(handle.controller, dest)
        else:
            self._collect_machine(handle.controller, dest)

    def _collect_k8s(self, controller: str, dest: Path) -> None:
        kubeconfig_path = self._kubeconfig_path
        if kubeconfig_path is None:
            raise ValueError("kubeconfig_path must be set for Kubernetes crashdump collection")
        cmd = [
            "juju-k8s-crashdump",
            kubeconfig_path,
            controller,
            "--output_path",
            str(dest / f"{dest.name}.tar.gz"),
        ]
        self._logger.debug(f"running {' '.join(str(c) for c in cmd)}")
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

    def _collect_machine(self, controller: str, dest: Path) -> None:
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
            str(dest / f"{dest.name}.tar.gz"),
            "--as-root",
        ]
        self._logger.debug(f"running {' '.join(str(c) for c in cmd)}")
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
