# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess  # nosec B404
from pathlib import Path

from resource_registry.protocols import ResourceHandle

from .handles import JujuControllerHandle


class JujuCrashdumpCollector:
    """Collect controller logs using juju-crashdump or juju-k8s-crashdump.

    For Kubernetes substrates, set kubeconfig_path to the path of the kubeconfig
    file; juju-k8s-crashdump will be used.  For machine/OpenStack substrates leave
    kubeconfig_path as None; juju-crashdump will be used instead.
    """

    def __init__(
        self,
        logger: logging.Logger,
        kubeconfig_path: str | None = None,
    ) -> None:
        self._logger = logger
        self._kubeconfig_path = kubeconfig_path

    def supports(self, handle: ResourceHandle) -> bool:
        return isinstance(handle, JujuControllerHandle)

    def collect(self, handle: ResourceHandle, output_dir: Path) -> None:
        if not isinstance(handle, JujuControllerHandle):
            return

        dest = output_dir / handle.path_segment
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
            str(dest / "k8s-crashdump.tar.gz"),
        ]
        self._logger.debug(f"JujuCrashdumpCollector: running {' '.join(str(c) for c in cmd)}")
        try:
            result = subprocess.run(  # nosec B603
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.stdout:
                self._logger.debug(f"juju-k8s-crashdump stdout:\n{result.stdout}")
            if result.stderr:
                self._logger.debug(f"juju-k8s-crashdump stderr:\n{result.stderr}")
            if result.returncode != 0:
                self._logger.warning(
                    f"JujuCrashdumpCollector: juju-k8s-crashdump exited with code "
                    f"{result.returncode} for controller '{controller}'"
                )
        except FileNotFoundError:
            self._logger.warning("JujuCrashdumpCollector: juju-k8s-crashdump not found, skipping K8s crashdump")
        except subprocess.TimeoutExpired:
            self._logger.warning(f"JujuCrashdumpCollector: juju-k8s-crashdump timed out for controller '{controller}'")

    def _collect_machine(self, controller: str, dest: Path) -> None:
        cmd = [
            "juju-crashdump",
            "--model",
            f"{controller}:controller",
            "--timeout",
            "300",
            "--small",
            "-f",
            "100000000",
            "--compression",
            "gz",
            "--unit-dump-location",
            str(dest / "crashdump.tar.gz"),
            "--as-root",
        ]
        self._logger.debug(f"JujuCrashdumpCollector: running {' '.join(str(c) for c in cmd)}")
        try:
            result = subprocess.run(  # nosec B603
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.stdout:
                self._logger.debug(f"juju-crashdump stdout:\n{result.stdout}")
            if result.stderr:
                self._logger.debug(f"juju-crashdump stderr:\n{result.stderr}")
            if result.returncode != 0:
                self._logger.warning(
                    f"JujuCrashdumpCollector: juju-crashdump exited with code "
                    f"{result.returncode} for controller '{controller}'"
                )
        except FileNotFoundError:
            self._logger.warning("JujuCrashdumpCollector: juju-crashdump not found, skipping machine crashdump")
        except subprocess.TimeoutExpired:
            self._logger.warning(f"JujuCrashdumpCollector: juju-crashdump timed out for controller '{controller}'")
