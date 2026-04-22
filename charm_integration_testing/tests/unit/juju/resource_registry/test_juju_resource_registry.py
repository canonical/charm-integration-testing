# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from juju.client import JujuClient
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

    def test_path_segment_sanitizes_unsafe_chars(self) -> None:
        handle = JujuControllerHandle(controller="ctrl.with spaces/and:colons")
        assert handle.path_segment == "juju-controller-ctrl-with-spaces-and-colons"

    def test_frozen(self) -> None:
        handle = JujuControllerHandle(controller="my-ctrl")
        with pytest.raises(dataclasses.FrozenInstanceError):
            handle.controller = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: JujuCrashdumpCollector
# ---------------------------------------------------------------------------


class TestJujuCrashdumpCollectorSupports:
    def test_supports_controller_handle(self) -> None:
        collector = JujuCrashdumpCollector(LoggerStub(), output_dir=None)
        assert collector.supports(JujuControllerHandle(controller="ctrl")) is True

    def test_does_not_support_other_handle(self) -> None:
        @dataclass(frozen=True)
        class OtherHandle:
            name: str

            @property
            def resource_id(self) -> str:
                return f"other:{self.name}"

            @property
            def resource_type(self) -> str:
                return "other"

            @property
            def path_segment(self) -> str:
                return self.name

        collector = JujuCrashdumpCollector(LoggerStub(), output_dir=None)
        assert collector.supports(OtherHandle("other")) is False


class TestJujuCrashdumpCollectorMachine:
    def test_runs_juju_crashdump(self, tmp_path: Path) -> None:
        # GIVEN a machine collector (no kubeconfig)
        logger = LoggerStub()
        collector = JujuCrashdumpCollector(logger, output_dir=tmp_path, kubeconfig_path=None)
        handle = JujuControllerHandle(controller="my-ctrl")

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed) as mock_run:
            collector.collect(handle)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "juju-crashdump"
        assert "my-ctrl:controller" in cmd
        assert any("juju-controller-my-ctrl" in str(arg) for arg in cmd)

    def test_raises_on_nonzero_exit(self, tmp_path: Path) -> None:
        # GIVEN crashdump exits non-zero
        collector = JujuCrashdumpCollector(LoggerStub(), output_dir=tmp_path, kubeconfig_path=None)
        handle = JujuControllerHandle(controller="my-ctrl")

        failed = subprocess.CompletedProcess(args=["juju-crashdump"], returncode=1, stdout="", stderr="err")
        with patch("subprocess.run", return_value=failed):
            with pytest.raises(subprocess.CalledProcessError):
                collector.collect(handle)

    def test_raises_on_tool_not_found(self, tmp_path: Path) -> None:
        # GIVEN juju-crashdump is not installed
        collector = JujuCrashdumpCollector(LoggerStub(), output_dir=tmp_path, kubeconfig_path=None)
        handle = JujuControllerHandle(controller="my-ctrl")

        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(FileNotFoundError):
                collector.collect(handle)

    def test_raises_on_timeout(self, tmp_path: Path) -> None:
        # GIVEN crashdump times out
        collector = JujuCrashdumpCollector(LoggerStub(), output_dir=tmp_path, kubeconfig_path=None)
        handle = JujuControllerHandle(controller="my-ctrl")

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="juju-crashdump", timeout=600)):
            with pytest.raises(subprocess.TimeoutExpired):
                collector.collect(handle)

    def test_skips_when_output_dir_is_none(self) -> None:
        # GIVEN a collector with no output_dir configured
        logger = LoggerStub()
        collector = JujuCrashdumpCollector(logger, output_dir=None, kubeconfig_path=None)
        handle = JujuControllerHandle(controller="my-ctrl")

        with patch("subprocess.run") as mock_run:
            collector.collect(handle)

        mock_run.assert_not_called()


class TestJujuCrashdumpCollectorK8s:
    def test_runs_juju_k8s_crashdump(self, tmp_path: Path) -> None:
        # GIVEN a k8s collector
        logger = LoggerStub()
        collector = JujuCrashdumpCollector(logger, output_dir=tmp_path, kubeconfig_path="/tmp/kubeconfig")
        handle = JujuControllerHandle(controller="my-ctrl")

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed) as mock_run:
            collector.collect(handle)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "juju-k8s-crashdump"
        assert "/tmp/kubeconfig" in cmd
        assert "my-ctrl" in cmd
        assert any("juju-controller-my-ctrl" in str(arg) for arg in cmd)

    def test_raises_on_tool_not_found(self, tmp_path: Path) -> None:
        collector = JujuCrashdumpCollector(LoggerStub(), output_dir=tmp_path, kubeconfig_path="/tmp/kubeconfig")
        handle = JujuControllerHandle(controller="my-ctrl")

        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(FileNotFoundError):
                collector.collect(handle)


# ---------------------------------------------------------------------------
# Tests: JujuResourceRegistryExtension (via JujuClient)
# ---------------------------------------------------------------------------


class TestJujuResourceRegistryExtensionPostBootstrap:
    def _make_registry(self) -> ResourceRegistry:
        return ResourceRegistry(global_collectors=[], logger=logging.getLogger("test"))

    def test_registers_controller_on_bootstrap(self) -> None:
        # GIVEN a JujuClient with the extension installed
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])

        # WHEN a controller is bootstrapped via the client
        client.bootstrap_controller(cloud="mycloud", controller="my-ctrl", controller_constraints={})

        # THEN the handle is registered in the registry
        handle = JujuControllerHandle(controller="my-ctrl")
        assert handle.resource_id in registry._entries

    def test_destroyer_calls_backend_kill(self) -> None:
        # GIVEN a bootstrapped controller tracked in the registry
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])
        client.bootstrap_controller(cloud="mycloud", controller="my-ctrl", controller_constraints={})

        # WHEN the registry tears down the handle
        handle = JujuControllerHandle(controller="my-ctrl")
        registry.teardown(handle)

        # THEN backend.kill_controller was called
        assert "my-ctrl" in backend.killed


class TestJujuResourceRegistryExtensionPreKill:
    def _make_registry(self) -> ResourceRegistry:
        return ResourceRegistry(global_collectors=[], logger=logging.getLogger("test"))

    def test_deregisters_controller_after_kill(self) -> None:
        # GIVEN a controller bootstrapped and tracked via JujuClient
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])
        client.bootstrap_controller(cloud="mycloud", controller="my-ctrl", controller_constraints={})

        handle = JujuControllerHandle(controller="my-ctrl")
        assert handle.resource_id in registry._entries

        # WHEN the controller is killed via the client
        client.kill_controller(controller="my-ctrl")

        # THEN the handle is removed from the registry (post_kill_controller fired)
        assert handle.resource_id not in registry._entries

    def test_kill_on_unregistered_controller_is_noop(self) -> None:
        # GIVEN a client with an empty registry (no prior bootstrap)
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])

        # WHEN killing a controller that was never registered
        client.kill_controller(controller="ghost-ctrl")  # should not raise
