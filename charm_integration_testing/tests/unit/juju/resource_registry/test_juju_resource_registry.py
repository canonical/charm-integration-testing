# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from juju.client import JujuClient
from juju.resource_registry.collectors import JujuCrashdumpCollector
from juju.resource_registry.extension import JujuResourceRegistryExtension
from juju.resource_registry.handles import JujuControllerHandle, JujuModelHandle
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
    models_added: list[tuple[str, str]] = field(default_factory=list)
    status_calls: list[str] = field(default_factory=list)

    def bootstrap_controller(
        self,
        cloud: str,
        controller: str,
        controller_constraints: dict[str, str],
        bootstrap_configuration: dict[str, str],
        metadata_source: Path | None = None,
        agent_version: str | None = None,
    ) -> None:
        self.bootstrapped.append(controller)

    def kill_controller(self, controller: str) -> None:
        self.killed.append(controller)

    def add_model(self, controller: str, model: str, model_config: dict[str, str]) -> None:
        self.models_added.append((controller, model))

    def juju_status_text(self, model: str) -> str:
        self.status_calls.append(model)
        return f"status for {model}"

    def migrate_model(self, model_name: str, source_controller: str, target_controller: str) -> None:
        pass


@dataclass
class CrashdumpBackendStub(NullJujuBackend):
    """Backend stub for JujuCrashdumpCollector tests.

    k8s_controllers controls which controllers are treated as Kubernetes-based.
    kubeconfig is returned for any controller in k8s_controllers.
    """

    k8s_controllers: set[str] = field(default_factory=set)
    kubeconfig: Path | None = None

    def is_k8s_model(self, model: str) -> bool:
        controller = model.split(":")[0]
        return controller in self.k8s_controllers

    def get_controller_kubeconfig(self, controller: str) -> Path | None:
        if controller not in self.k8s_controllers:
            return None
        if self.kubeconfig is None:
            raise ValueError(f"Controller '{controller}' is K8s-based but no kubeconfig is configured")
        return self.kubeconfig


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
        collector = JujuCrashdumpCollector(LoggerStub(), CrashdumpBackendStub(), output_dir=None)
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

        collector = JujuCrashdumpCollector(LoggerStub(), CrashdumpBackendStub(), output_dir=None)
        assert collector.supports(OtherHandle("other")) is False


class TestJujuCrashdumpCollectorMachine:
    def test_runs_juju_crashdump(self, tmp_path: Path) -> None:
        # GIVEN a machine cloud collector
        logger = LoggerStub()
        backend = CrashdumpBackendStub()  # my-ctrl not in k8s_controllers -> machine
        collector = JujuCrashdumpCollector(logger, backend, output_dir=tmp_path)
        handle = JujuControllerHandle(controller="my-ctrl")

        # WHEN juju-crashdump succeeds and creates the output file
        def run_side_effect(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            cmd = args[0]
            assert isinstance(cmd, list)
            # Simulate juju-crashdump writing juju-crashdump-<uniq>.tar.gz to -o <dir>
            if "-o" in cmd and "-u" in cmd:
                output_dir = Path(cmd[cmd.index("-o") + 1])
                uniq = cmd[cmd.index("-u") + 1]
                (output_dir / f"juju-crashdump-{uniq}.tar.gz").touch()
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=run_side_effect) as mock_run:
            collector.collect(handle)

        # THEN juju-crashdump was called once with the correct args
        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "juju-crashdump"
        assert "my-ctrl:controller" in cmd
        assert "-o" in cmd
        assert "-u" in cmd
        assert cmd[cmd.index("-u") + 1] == "my-ctrl"

    def test_raises_on_nonzero_exit(self, tmp_path: Path) -> None:
        # GIVEN crashdump exits non-zero
        backend = CrashdumpBackendStub()
        collector = JujuCrashdumpCollector(LoggerStub(), backend, output_dir=tmp_path)
        handle = JujuControllerHandle(controller="my-ctrl")

        failed = subprocess.CompletedProcess(args=["juju-crashdump"], returncode=1, stdout="", stderr="err")
        with patch("subprocess.run", return_value=failed):
            with pytest.raises(subprocess.CalledProcessError):
                collector.collect(handle)

    def test_raises_when_archive_not_created(self, tmp_path: Path) -> None:
        # GIVEN juju-crashdump exits 0 but does not write the expected archive
        backend = CrashdumpBackendStub()
        collector = JujuCrashdumpCollector(LoggerStub(), backend, output_dir=tmp_path)
        handle = JujuControllerHandle(controller="my-ctrl")

        success = subprocess.CompletedProcess(args=["juju-crashdump"], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=success):
            with pytest.raises(FileNotFoundError, match="my-ctrl"):
                collector.collect(handle)

    def test_raises_when_tool_not_found(self, tmp_path: Path) -> None:
        # GIVEN juju-crashdump is not installed
        backend = CrashdumpBackendStub()
        collector = JujuCrashdumpCollector(LoggerStub(), backend, output_dir=tmp_path)
        handle = JujuControllerHandle(controller="my-ctrl")

        with patch("subprocess.run", side_effect=FileNotFoundError("juju-crashdump not found")):
            with pytest.raises(FileNotFoundError):
                collector.collect(handle)

    def test_raises_on_timeout(self, tmp_path: Path) -> None:
        # GIVEN crashdump times out
        backend = CrashdumpBackendStub()
        collector = JujuCrashdumpCollector(LoggerStub(), backend, output_dir=tmp_path)
        handle = JujuControllerHandle(controller="my-ctrl")

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="juju-crashdump", timeout=600)):
            with pytest.raises(subprocess.TimeoutExpired):
                collector.collect(handle)

    def test_skips_when_output_dir_is_none(self) -> None:
        # GIVEN a collector with no output_dir configured
        logger = LoggerStub()
        backend = CrashdumpBackendStub()
        collector = JujuCrashdumpCollector(logger, backend, output_dir=None)
        handle = JujuControllerHandle(controller="my-ctrl")

        with patch("subprocess.run") as mock_run:
            collector.collect(handle)

        mock_run.assert_not_called()


class TestJujuCrashdumpCollectorK8s:
    def test_runs_juju_k8s_crashdump(self, tmp_path: Path) -> None:
        # GIVEN a K8s controller collector
        logger = LoggerStub()
        kubeconfig = Path("/tmp/kubeconfig")
        backend = CrashdumpBackendStub(k8s_controllers={"my-ctrl"}, kubeconfig=kubeconfig)
        collector = JujuCrashdumpCollector(logger, backend, output_dir=tmp_path)
        handle = JujuControllerHandle(controller="my-ctrl")

        k8s_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=k8s_result) as mock_run:
            collector.collect(handle)

        # THEN juju-k8s-crashdump was called once
        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "juju-k8s-crashdump"
        assert str(kubeconfig.resolve()) in cmd
        assert "my-ctrl" in cmd

    def test_raises_when_tool_not_found(self, tmp_path: Path) -> None:
        # GIVEN juju-k8s-crashdump is not installed
        kubeconfig = Path("/tmp/kubeconfig")
        backend = CrashdumpBackendStub(k8s_controllers={"my-ctrl"}, kubeconfig=kubeconfig)
        collector = JujuCrashdumpCollector(LoggerStub(), backend, output_dir=tmp_path)
        handle = JujuControllerHandle(controller="my-ctrl")

        with patch("subprocess.run", side_effect=FileNotFoundError("juju-k8s-crashdump not found")):
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
        client.bootstrap_controller(
            cloud="mycloud", controller="my-ctrl", controller_constraints={}, bootstrap_configuration={}
        )

        # THEN the handle is registered in the registry
        handle = JujuControllerHandle(controller="my-ctrl")
        assert handle.resource_id in registry._entries

    def test_destroyer_calls_backend_kill(self) -> None:
        # GIVEN a bootstrapped controller tracked in the registry
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])
        client.bootstrap_controller(
            cloud="mycloud", controller="my-ctrl", controller_constraints={}, bootstrap_configuration={}
        )

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
        client.bootstrap_controller(
            cloud="mycloud", controller="my-ctrl", controller_constraints={}, bootstrap_configuration={}
        )

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


# ---------------------------------------------------------------------------
# Tests: JujuModelHandle
# ---------------------------------------------------------------------------


class TestJujuModelHandle:
    def test_resource_id(self) -> None:
        handle = JujuModelHandle(controller="my-ctrl", model="my-model")
        assert handle.resource_id == "juju:model:my-ctrl:my-model"

    def test_resource_type(self) -> None:
        handle = JujuModelHandle(controller="my-ctrl", model="my-model")
        assert handle.resource_type == "juju:model"

    def test_path_segment(self) -> None:
        handle = JujuModelHandle(controller="my-ctrl", model="my-model")
        assert handle.path_segment == "juju-model-my-ctrl-my-model"

    def test_path_segment_sanitizes_unsafe_chars(self) -> None:
        handle = JujuModelHandle(controller="ctrl.with spaces", model="model/with:colons")
        assert handle.path_segment == "juju-model-ctrl-with-spaces-model-with-colons"

    def test_frozen(self) -> None:
        handle = JujuModelHandle(controller="my-ctrl", model="my-model")
        with pytest.raises(dataclasses.FrozenInstanceError):
            handle.model = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: JujuResourceRegistryExtension - post_add_model
# ---------------------------------------------------------------------------


class TestJujuResourceRegistryExtensionPostAddModel:
    def _make_registry(self) -> ResourceRegistry:
        return ResourceRegistry(global_collectors=[], logger=logging.getLogger("test"))

    def test_registers_pre_existing_controller_and_model_on_add_model(self) -> None:
        # GIVEN a registry with no bootstrapped controller (pre-existing session)
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])

        # WHEN a model is added for a controller that was never bootstrapped in this session
        client.add_model(controller="pre-existing-ctrl", model="my-model", model_config={})

        # THEN the controller is registered with no destroyer, and the model is registered as its child
        controller_handle = JujuControllerHandle(controller="pre-existing-ctrl")
        model_handle = JujuModelHandle(controller="pre-existing-ctrl", model="my-model")
        assert controller_handle.resource_id in registry._entries
        assert registry._entries[controller_handle.resource_id].destroyer is None
        assert model_handle.resource_id in registry._entries
        assert registry._entries[model_handle.resource_id].parent_id == controller_handle.resource_id

    def test_registers_model_on_add_model(self) -> None:
        # GIVEN a client with the extension, and a bootstrapped controller
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])
        client.bootstrap_controller(
            cloud="mycloud", controller="my-ctrl", controller_constraints={}, bootstrap_configuration={}
        )

        # WHEN a model is added
        client.add_model(controller="my-ctrl", model="my-model", model_config={})

        # THEN the model handle is registered with the controller as parent
        model_handle = JujuModelHandle(controller="my-ctrl", model="my-model")
        assert model_handle.resource_id in registry._entries
        assert registry._entries[model_handle.resource_id].parent_id == "juju:controller:my-ctrl"

    def test_model_is_child_of_controller(self) -> None:
        # GIVEN a bootstrapped controller with a model
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])
        client.bootstrap_controller(
            cloud="mycloud", controller="my-ctrl", controller_constraints={}, bootstrap_configuration={}
        )
        client.add_model(controller="my-ctrl", model="my-model", model_config={})

        # WHEN querying children of the controller
        controller_handle = JujuControllerHandle(controller="my-ctrl")
        children = registry.children_of(controller_handle)

        # THEN the model is listed as a child
        assert JujuModelHandle(controller="my-ctrl", model="my-model") in children

    def test_kill_controller_deregisters_models(self) -> None:
        # GIVEN a controller with two models
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])
        client.bootstrap_controller(
            cloud="mycloud", controller="my-ctrl", controller_constraints={}, bootstrap_configuration={}
        )
        client.add_model(controller="my-ctrl", model="model-a", model_config={})
        client.add_model(controller="my-ctrl", model="model-b", model_config={})

        # WHEN the controller is killed
        client.kill_controller(controller="my-ctrl")

        # THEN all models and the controller are deregistered
        assert registry._entries == {}

    def test_pre_kill_collects_model_logs_before_controller_logs(self) -> None:
        # GIVEN a controller with a model, and a collector tracking calls
        collected_order: list[str] = []

        class OrderTrackingCollector:
            def supports(self, handle: Any) -> bool:
                return True

            def collect(self, handle: Any) -> None:
                collected_order.append(handle.resource_id)

        backend = BootstrapKillBackendStub()
        registry = ResourceRegistry(global_collectors=[OrderTrackingCollector()], logger=logging.getLogger("test"))
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])
        client.bootstrap_controller(
            cloud="mycloud", controller="my-ctrl", controller_constraints={}, bootstrap_configuration={}
        )
        client.add_model(controller="my-ctrl", model="my-model", model_config={})

        # WHEN the controller is killed (pre_kill_controller fires)
        client.kill_controller(controller="my-ctrl")

        # THEN model logs are collected before controller logs
        assert collected_order == [
            "juju:model:my-ctrl:my-model",
            "juju:controller:my-ctrl",
        ]


# ---------------------------------------------------------------------------
# Tests: JujuResourceRegistryExtension - post_migrate_model
# ---------------------------------------------------------------------------


class TestJujuResourceRegistryExtensionPostMigrateModel:
    def _make_registry(self) -> ResourceRegistry:
        return ResourceRegistry(global_collectors=[], logger=logging.getLogger("test"))

    def test_model_handle_moves_to_target_controller(self) -> None:
        # GIVEN a bootstrapped source controller with a model
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])
        client.bootstrap_controller(
            cloud="mycloud", controller="src-ctrl", controller_constraints={}, bootstrap_configuration={}
        )
        client.add_model(controller="src-ctrl", model="my-model", model_config={})

        # WHEN the model is migrated to a new controller
        client.migrate_model(model_name="my-model", source_controller="src-ctrl", target_controller="dst-ctrl")

        # THEN the old handle is gone and the new one is present under the target controller
        assert JujuModelHandle(controller="src-ctrl", model="my-model").resource_id not in registry._entries
        assert JujuModelHandle(controller="dst-ctrl", model="my-model").resource_id in registry._entries
        assert (
            registry._entries[JujuModelHandle(controller="dst-ctrl", model="my-model").resource_id].parent_id
            == JujuControllerHandle(controller="dst-ctrl").resource_id
        )

    def test_target_controller_auto_registered_with_no_destroyer(self) -> None:
        # GIVEN a source controller with a model; target controller is unknown
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])
        client.bootstrap_controller(
            cloud="mycloud", controller="src-ctrl", controller_constraints={}, bootstrap_configuration={}
        )
        client.add_model(controller="src-ctrl", model="my-model", model_config={})

        # WHEN migrating to a controller that was never bootstrapped in this session
        client.migrate_model(model_name="my-model", source_controller="src-ctrl", target_controller="dst-ctrl")

        # THEN the target controller is registered with no destroyer
        dst_handle = JujuControllerHandle(controller="dst-ctrl")
        assert dst_handle.resource_id in registry._entries
        assert registry._entries[dst_handle.resource_id].destroyer is None

    def test_migrate_to_already_registered_controller(self) -> None:
        # GIVEN two bootstrapped controllers, each with a model
        backend = BootstrapKillBackendStub()
        registry = self._make_registry()
        ext = JujuResourceRegistryExtension(backend, registry)
        client = JujuClient(backend, LoggerStub(), [ext])
        client.bootstrap_controller(
            cloud="mycloud", controller="src-ctrl", controller_constraints={}, bootstrap_configuration={}
        )
        client.bootstrap_controller(
            cloud="mycloud", controller="dst-ctrl", controller_constraints={}, bootstrap_configuration={}
        )
        client.add_model(controller="src-ctrl", model="my-model", model_config={})

        # WHEN migrating to the already-registered target controller
        client.migrate_model(model_name="my-model", source_controller="src-ctrl", target_controller="dst-ctrl")

        # THEN the target controller is still registered (not double-registered or reset)
        dst_handle = JujuControllerHandle(controller="dst-ctrl")
        assert dst_handle.resource_id in registry._entries
        # Model is now under the target controller
        assert JujuModelHandle(controller="dst-ctrl", model="my-model").resource_id in registry._entries
