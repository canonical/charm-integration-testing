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
        kubeconfig_path: Path | None = None,
        controller_allowlist: set[str] | None = None,
    ) -> None:
        self._logger = logger.getChild(type(self).__name__)
        self._output_dir = output_dir
        self._kubeconfig_path = kubeconfig_path
        self._controller_allowlist = controller_allowlist

    def supports(self, handle: ResourceHandle) -> bool:
        if not isinstance(handle, JujuControllerHandle):
            return False
        if self._controller_allowlist is not None:
            return handle.controller in self._controller_allowlist
        return True

    def collect(self, handle: ResourceHandle) -> None:
        if not isinstance(handle, JujuControllerHandle):
            return
        if self._output_dir is None:
            self._logger.debug(f"Output_dir is None, skipping log collection for '{handle.controller}'")
            return

        output_dir = self._output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        if self._kubeconfig_path is not None:
            self._collect_k8s(handle.controller, output_dir / f"{handle.path_segment}.tar.gz")
        else:
            self._collect_machine(handle.controller, output_dir / f"{handle.path_segment}.tar.gz")

    def _collect_k8s(self, controller: str, output_path: Path) -> None:
        kubeconfig_path = self._kubeconfig_path
        if kubeconfig_path is None:
            raise ValueError("kubeconfig_path must be set for Kubernetes crashdump collection")
        cmd = [
            "juju-k8s-crashdump",
            str(kubeconfig_path.resolve()),
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
        # juju-crashdump writes the archive to output_dir as "juju-crashdump-{uniq}.tar.{compression}".
        # We derive uniq from the expected output filename so we can rename the result afterwards.
        uniq = output_path.name.removesuffix(".tar.gz")
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
            "--uniq",
            uniq,
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
        # Rename from juju-crashdump's default naming scheme to the expected output_path.
        created = output_path.parent / f"juju-crashdump-{uniq}.tar.gz"
        created.rename(output_path)
