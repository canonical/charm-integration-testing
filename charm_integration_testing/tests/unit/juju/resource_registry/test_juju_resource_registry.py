# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from juju.resource_registry.collectors import JujuCrashdumpCollector
from juju.resource_registry.extension import JujuResourceRegistryExtension
from juju.resource_registry.handles import JujuControllerHandle
from resource_registry.registry import ResourceRegistry

from ...extensions.shared import NullJujuBackend

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class LoggerStub(logging.Logger):
    """Minimal logger subclass that records warning messages."""

    def __init__(self) -> None:
        super().__init__("test")
        self.warnings: list[str] = []

    def warning(self, msg: object, *args: object, **kwargs: object) -> None:
        self.warnings.append(str(msg))


@dataclass
class BootstrapKillBackendStub(NullJujuBackend):
    bootstrapped: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)

    def bootstrap_controller(
        self,
        cloud: str,
        controller: str,
        controller_constraints: dict[str, str],
        agent_version: str | None = None,
    ) -> None:
        self.bootstrapped.append(controller)

    def kill_controller(self, controller: str) -> None:
        self.killed.append(controller)


# ---------------------------------------------------------------------------
# Tests: JujuControllerHandle
# ---------------------------------------------------------------------------


class TestJujuControllerHandle:
    def test_resource_id(self) -> None:
        handle = JujuControllerHandle(controller="my-ctrl")
        assert handle.resource_id == "juju:controller:my-ctrl"

    def test_resource_type(self) -> None:
        handle = JujuControllerHandle(controller="my-ctrl")
        assert handle.resource_type == "juju:controller"

    def test_path_segment(self) -> None:
        handle = JujuControllerHandle(controller="my-ctrl")
        assert handle.path_segment == "juju-controller-my-ctrl"

    def test_frozen(self) -> None:
        handle = JujuControllerHandle(controller="my-ctrl")
        with pytest.raises(Exception):
            handle.controller = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: JujuCrashdumpCollector
# ---------------------------------------------------------------------------


class TestJujuCrashdumpCollectorSupports:
    def test_supports_controller_handle(self) -> None:
        collector = JujuCrashdumpCollector(LoggerStub())
        assert collector.supports(JujuControllerHandle(controller="ctrl")) is True

    def test_does_not_support_other_handle(self) -> None:
        from ...resource_registry.test_registry import HandleStub

        collector = JujuCrashdumpCollector(LoggerStub())
        assert collector.supports(HandleStub("other")) is False


class TestJujuCrashdumpCollectorMachine:
    def test_runs_juju_crashdump(self, tmp_path: Path) -> None:
        # GIVEN a machine collector (no kubeconfig)
        logger = LoggerStub()
        collector = JujuCrashdumpCollector(logger, kubeconfig_path=None)
        handle = JujuControllerHandle(controller="my-ctrl")

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed) as mock_run:
            collector.collect(handle, tmp_path)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "juju-crashdump"
        assert "my-ctrl:controller" in cmd

    def test_warns_on_nonzero_exit(self, tmp_path: Path) -> None:
        # GIVEN crashdump exits non-zero
        logger = LoggerStub()
        collector = JujuCrashdumpCollector(logger, kubeconfig_path=None)
        handle = JujuControllerHandle(controller="my-ctrl")

        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="err")
        with patch("subprocess.run", return_value=failed):
            collector.collect(handle, tmp_path)

        assert any("exited with code 1" in w for w in logger.warnings)

    def test_warns_on_tool_not_found(self, tmp_path: Path) -> None:
        # GIVEN juju-crashdump is not installed
        logger = LoggerStub()
        collector = JujuCrashdumpCollector(logger, kubeconfig_path=None)
        handle = JujuControllerHandle(controller="my-ctrl")

        with patch("subprocess.run", side_effect=FileNotFoundError):
            collector.collect(handle, tmp_path)

        assert any("not found" in w for w in logger.warnings)

    def test_warns_on_timeout(self, tmp_path: Path) -> None:
        # GIVEN crashdump times out
        logger = LoggerStub()
        collector = JujuCrashdumpCollector(logger, kubeconfig_path=None)
        handle = JujuControllerHandle(controller="my-ctrl")

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="juju-crashdump", timeout=600)):
            collector.collect(handle, tmp_path)

        assert any("timed out" in w for w in logger.warnings)


class TestJujuCrashdumpCollectorK8s:
    def test_runs_juju_k8s_crashdump(self, tmp_path: Path) -> None:
        # GIVEN a k8s collector
        logger = LoggerStub()
        collector = JujuCrashdumpCollector(logger, kubeconfig_path="/tmp/kubeconfig")
        handle = JujuControllerHandle(controller="my-ctrl")

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed) as mock_run:
            collector.collect(handle, tmp_path)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "juju-k8s-crashdump"
        assert "/tmp/kubeconfig" in cmd
        assert "my-ctrl" in cmd

    def test_warns_on_tool_not_found(self, tmp_path: Path) -> None:
        logger = LoggerStub()
        collector = JujuCrashdumpCollector(logger, kubeconfig_path="/tmp/kubeconfig")
        handle = JujuControllerHandle(controller="my-ctrl")

        with patch("subprocess.run", side_effect=FileNotFoundError):
            collector.collect(handle, tmp_path)

        assert any("not found" in w for w in logger.warnings)


# ---------------------------------------------------------------------------
# Tests: JujuResourceRegistryExtension
# ---------------------------------------------------------------------------


class TestJujuResourceRegistryExtensionPostBootstrap:
    def _make_registry(self) -> ResourceRegistry:
        return ResourceRegistry(global_collectors=[], logger=logging.getLogger("test"))

    def test_registers_controller_on_bootstrap(self) -> None:
        # GIVEN an extension wired to a registry
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry, log_dir=None)

        # WHEN a controller is bootstrapped
        ext.post_bootstrap_controller("my-ctrl")

        # THEN the handle is registered
        handle = JujuControllerHandle(controller="my-ctrl")
        assert handle.resource_id in registry._entries

    def test_destroyer_calls_backend_kill(self) -> None:
        # GIVEN an extension wired to a registry
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry, log_dir=None)
        ext.post_bootstrap_controller("my-ctrl")

        # WHEN the destroyer registered in the registry is invoked
        entry = registry._entries[JujuControllerHandle(controller="my-ctrl").resource_id]
        assert entry.destroyer is not None
        entry.destroyer()

        # THEN backend.kill_controller was called
        assert "my-ctrl" in backend.killed


class TestJujuResourceRegistryExtensionPreKill:
    def _make_registry(self) -> ResourceRegistry:
        return ResourceRegistry(global_collectors=[], logger=logging.getLogger("test"))

    def test_deregisters_controller_before_kill(self) -> None:
        # GIVEN a registered controller
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry, log_dir=None)
        ext.post_bootstrap_controller("my-ctrl")

        handle = JujuControllerHandle(controller="my-ctrl")
        assert handle.resource_id in registry._entries

        # WHEN pre_kill_controller fires
        ext.pre_kill_controller("my-ctrl")

        # THEN the handle is removed from the registry
        assert handle.resource_id not in registry._entries

    def test_pre_kill_on_unregistered_controller_is_noop(self) -> None:
        # GIVEN an extension with an empty registry
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry, log_dir=None)

        # WHEN pre_kill_controller fires for a controller never registered
        ext.pre_kill_controller("ghost-ctrl")  # should not raise
