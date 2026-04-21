# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from resource_registry.protocols import LogCollector, ResourceHandle
from resource_registry.registry import ResourceRegistry, ResourceTeardownWarning

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HandleStub:
    name: str

    @property
    def resource_id(self) -> str:
        return f"stub:{self.name}"

    @property
    def resource_type(self) -> str:
        return "stub"

    @property
    def path_segment(self) -> str:
        return self.name


class CollectorStub:
    def __init__(self, supported: bool = True) -> None:
        self._supported = supported
        self.collected: list[ResourceHandle] = []

    def supports(self, handle: ResourceHandle) -> bool:
        return self._supported

    def collect(self, handle: ResourceHandle) -> None:
        self.collected.append(handle)


class FailingCollector:
    def supports(self, handle: ResourceHandle) -> bool:
        return True

    def collect(self, handle: ResourceHandle) -> None:
        raise RuntimeError("collection failed")


def _registry(global_collectors: list[LogCollector] | None = None) -> ResourceRegistry:
    return ResourceRegistry(
        global_collectors=global_collectors or [],
        logger=logging.getLogger("test"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResourceRegistryRegister:
    def test_register_adds_entry(self) -> None:
        # GIVEN an empty registry
        registry = _registry()
        handle = HandleStub("ctrl")

        # WHEN a resource is registered
        registry.register(handle, destroyer=None)

        # THEN the entry exists
        assert handle.resource_id in registry._entries

    def test_register_with_parent_tracks_child(self) -> None:
        # GIVEN a registry with a parent resource
        registry = _registry()
        parent = HandleStub("parent")
        child = HandleStub("child")
        registry.register(parent)

        # WHEN a child is registered
        registry.register(child, parent=parent)

        # THEN the child is listed under the parent
        assert child.resource_id in registry._children[parent.resource_id]


class TestResourceRegistryDeregister:
    def test_deregister_removes_entry(self) -> None:
        # GIVEN a registered resource
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle)

        # WHEN deregistered
        registry.deregister(handle)

        # THEN the entry is gone
        assert handle.resource_id not in registry._entries

    def test_deregister_unknown_handle_is_noop(self) -> None:
        # GIVEN an empty registry
        registry = _registry()

        # WHEN deregistering a handle that was never registered
        registry.deregister(HandleStub("ghost"))  # should not raise


class TestResourceRegistryCollectLogs:
    def test_collect_calls_global_collector(self, tmp_path: Path) -> None:
        # GIVEN a registry with a global collector
        collector = CollectorStub()
        registry = _registry(global_collectors=[collector])
        handle = HandleStub("ctrl")
        registry.register(handle)

        # WHEN logs are collected
        registry.collect_logs(handle)

        # THEN the collector was called once with the handle
        assert len(collector.collected) == 1
        assert collector.collected[0] == handle

    def test_collect_skipped_for_unknown_handle(self, tmp_path: Path) -> None:
        # GIVEN an empty registry
        collector = CollectorStub()
        registry = _registry(global_collectors=[collector])

        # WHEN collecting for a handle that was never registered
        registry.collect_logs(HandleStub("ghost"))

        # THEN collector is never called
        assert collector.collected == []

    def test_unsupported_collector_not_called(self, tmp_path: Path) -> None:
        # GIVEN a global collector that does not support the handle type
        collector = CollectorStub(supported=False)
        registry = _registry(global_collectors=[collector])
        handle = HandleStub("ctrl")
        registry.register(handle)

        # WHEN collecting
        registry.collect_logs(handle)

        # THEN the collector is not invoked
        assert collector.collected == []

    def test_failing_collector_is_swallowed(self, tmp_path: Path) -> None:
        # GIVEN a collector that raises
        registry = _registry(global_collectors=[FailingCollector()])
        handle = HandleStub("ctrl")
        registry.register(handle)

        # WHEN collecting - should not raise
        registry.collect_logs(handle)

    def test_per_resource_collector_is_called(self, tmp_path: Path) -> None:
        # GIVEN a resource registered with its own collector
        per_resource_collector = CollectorStub()
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle, collectors=[per_resource_collector])

        # WHEN collecting
        registry.collect_logs(handle)

        # THEN the per-resource collector is called
        assert len(per_resource_collector.collected) == 1


class TestResourceRegistryTeardown:
    def test_teardown_calls_destroyer(self, tmp_path: Path) -> None:
        # GIVEN a registered resource with a destroyer
        destroyed: list[str] = []
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle, destroyer=lambda: destroyed.append(handle.name))

        # WHEN torn down
        registry.teardown(handle)

        # THEN the destroyer was called
        assert destroyed == ["ctrl"]

    def test_teardown_deregisters_handle(self, tmp_path: Path) -> None:
        # GIVEN a registered resource
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle, destroyer=None)

        # WHEN torn down
        registry.teardown(handle)

        # THEN the handle is no longer registered
        assert handle.resource_id not in registry._entries

    def test_teardown_child_before_parent(self, tmp_path: Path) -> None:
        # GIVEN a parent with a child
        order: list[str] = []
        registry = _registry()
        parent = HandleStub("parent")
        child = HandleStub("child")
        registry.register(parent, destroyer=lambda: order.append("parent"))
        registry.register(child, destroyer=lambda: order.append("child"), parent=parent)

        # WHEN the parent is torn down
        registry.teardown(parent)

        # THEN child is destroyed before parent
        assert order == ["child", "parent"]

    def test_teardown_failing_destroyer_emits_warning(self, tmp_path: Path) -> None:
        # GIVEN a resource with a failing destroyer
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle, destroyer=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        # WHEN torn down
        with pytest.warns(ResourceTeardownWarning, match="Destruction failed"):
            registry.teardown(handle)

    def test_teardown_unknown_handle_is_noop(self, tmp_path: Path) -> None:
        # GIVEN an empty registry
        registry = _registry()

        # WHEN tearing down a handle that was never registered - should not raise
        registry.teardown(HandleStub("ghost"))


class TestResourceRegistryTeardownAll:
    def test_teardown_all_reverse_registration_order(self, tmp_path: Path) -> None:
        # GIVEN three resources registered in order A, B, C
        order: list[str] = []
        registry = _registry()
        for name in ["A", "B", "C"]:
            handle = HandleStub(name)

            def make_destroyer(n: str) -> Callable[[], None]:
                return lambda: order.append(n)

            registry.register(handle, destroyer=make_destroyer(name))

        # WHEN teardown_all is called
        registry.teardown_all()

        # THEN destruction happens in reverse order
        assert order == ["C", "B", "A"]

    def test_teardown_all_calls_collectors(self) -> None:
        # GIVEN a registry with a global collector and a registered resource
        collector = CollectorStub()
        registry = _registry(global_collectors=[collector])
        registry.register(HandleStub("ctrl"))

        # WHEN torn down
        registry.teardown_all()

        # THEN the collector was called
        assert len(collector.collected) == 1

    def test_teardown_all_emits_warning_on_destroyer_failure(self, tmp_path: Path) -> None:
        # GIVEN a resource with a failing destroyer
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle, destroyer=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        # WHEN teardown_all is called
        with pytest.warns(ResourceTeardownWarning):
            registry.teardown_all()

    def test_teardown_all_continues_after_failure(self, tmp_path: Path) -> None:
        # GIVEN two resources, the first of which fails destruction
        destroyed: list[str] = []
        registry = _registry()
        registry.register(HandleStub("A"), destroyer=lambda: destroyed.append("A"))
        registry.register(HandleStub("B"), destroyer=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceTeardownWarning)
            registry.teardown_all()

        # THEN A is still destroyed despite B failing
        assert "A" in destroyed
