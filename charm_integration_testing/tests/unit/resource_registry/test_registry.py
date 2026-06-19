# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import warnings
from dataclasses import dataclass
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

    def test_register_duplicate_raises(self) -> None:
        # GIVEN a handle that is already registered
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle)

        # WHEN registering the same handle again
        with pytest.raises(ValueError, match="already registered"):
            registry.register(handle)

    def test_register_with_unknown_parent_raises(self) -> None:
        # GIVEN an empty registry
        registry = _registry()
        parent = HandleStub("parent")
        child = HandleStub("child")

        # WHEN a child is registered with a parent that was never registered
        with pytest.raises(ValueError, match="not registered"):
            registry.register(child, parent=parent)


class TestResourceRegistryIsRegistered:
    def test_returns_true_for_registered_handle(self) -> None:
        # GIVEN a registered handle
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle)

        # WHEN checking registration
        assert registry.is_registered(handle) is True

    def test_returns_false_for_unregistered_handle(self) -> None:
        # GIVEN an empty registry
        registry = _registry()

        # WHEN checking an unregistered handle
        assert registry.is_registered(HandleStub("ghost")) is False

    def test_returns_false_after_deregister(self) -> None:
        # GIVEN a handle that has been deregistered
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle)
        registry.deregister(handle)

        # WHEN checking registration
        assert registry.is_registered(handle) is False


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

    def test_deregister_parent_reparents_children_to_root(self) -> None:
        # GIVEN a parent with a child
        registry = _registry()
        parent = HandleStub("parent")
        child = HandleStub("child")
        registry.register(parent)
        registry.register(child, parent=parent)

        # WHEN the parent is deregistered without teardown
        registry.deregister(parent)

        # THEN the child is still in the registry with no parent
        assert child.resource_id in registry._entries
        assert registry._entries[child.resource_id].parent_id is None


class TestResourceRegistryCollectLogs:
    def test_collect_calls_global_collector(self) -> None:
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

    def test_collect_skipped_for_unknown_handle(self) -> None:
        # GIVEN an empty registry
        collector = CollectorStub()
        registry = _registry(global_collectors=[collector])

        # WHEN collecting for a handle that was never registered
        registry.collect_logs(HandleStub("ghost"))

        # THEN collector is never called
        assert collector.collected == []

    def test_unsupported_collector_not_called(self) -> None:
        # GIVEN a global collector that does not support the handle type
        collector = CollectorStub(supported=False)
        registry = _registry(global_collectors=[collector])
        handle = HandleStub("ctrl")
        registry.register(handle)

        # WHEN collecting
        registry.collect_logs(handle)

        # THEN the collector is not invoked
        assert collector.collected == []

    def test_failing_collector_raises(self) -> None:
        # GIVEN a collector that raises
        registry = _registry(global_collectors=[FailingCollector()])
        handle = HandleStub("ctrl")
        registry.register(handle)

        # WHEN collecting - the exception propagates
        with pytest.raises(RuntimeError, match="collection failed"):
            registry.collect_logs(handle)

    def test_per_resource_collector_is_called(self) -> None:
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
    def test_teardown_calls_destroyer(self) -> None:
        # GIVEN a registered resource with a destroyer
        destroyed: list[str] = []
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle, destroyer=lambda: destroyed.append(handle.name))

        # WHEN torn down
        registry.teardown(handle)

        # THEN the destroyer was called
        assert destroyed == ["ctrl"]

    def test_teardown_deregisters_handle(self) -> None:
        # GIVEN a registered resource
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle, destroyer=None)

        # WHEN torn down
        registry.teardown(handle)

        # THEN the handle is no longer registered
        assert handle.resource_id not in registry._entries

    def test_teardown_child_before_parent(self) -> None:
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

    def test_teardown_failing_destroyer_raises(self) -> None:
        # GIVEN a resource with a failing destroyer
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle, destroyer=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        # WHEN torn down - the exception propagates
        with pytest.raises(RuntimeError, match="boom"):
            registry.teardown(handle)

        # THEN the resource is still deregistered
        assert handle.resource_id not in registry._entries

    def test_teardown_unknown_handle_is_noop(self) -> None:
        # GIVEN an empty registry
        registry = _registry()

        # WHEN tearing down a handle that was never registered - should not raise
        registry.teardown(HandleStub("ghost"))

    def test_teardown_destroys_even_when_collect_logs_fails(self) -> None:
        # GIVEN a resource with a failing collector and a destroyer
        destroyed: list[str] = []
        registry = _registry(global_collectors=[FailingCollector()])
        handle = HandleStub("ctrl")
        registry.register(handle, destroyer=lambda: destroyed.append(handle.name))

        # WHEN torn down - the log collection exception propagates
        with pytest.raises(RuntimeError, match="collection failed"):
            registry.teardown(handle)

        # THEN the resource was still destroyed and deregistered
        assert destroyed == ["ctrl"]
        assert handle.resource_id not in registry._entries

    def test_teardown_destroy_exception_chains_log_exception(self) -> None:
        # GIVEN a resource where both log collection and destruction fail
        registry = _registry(global_collectors=[FailingCollector()])
        handle = HandleStub("ctrl")
        registry.register(handle, destroyer=lambda: (_ for _ in ()).throw(RuntimeError("destroy boom")))

        # WHEN torn down - destroy exception is raised, chained from log exception
        with pytest.raises(RuntimeError, match="destroy boom") as exc_info:
            registry.teardown(handle)

        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert "collection failed" in str(exc_info.value.__cause__)

        # AND the resource is still deregistered
        assert handle.resource_id not in registry._entries


class TestResourceRegistryTeardownAll:
    def test_teardown_all_reverse_registration_order(self) -> None:
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

    def test_teardown_all_emits_warning_on_destroyer_failure(self) -> None:
        # GIVEN a resource with a failing destroyer
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle, destroyer=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        # WHEN teardown_all is called - exception is surfaced as a warning
        with pytest.warns(ResourceTeardownWarning):
            registry.teardown_all()

    def test_teardown_all_continues_after_failure(self) -> None:
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


class TestResourceRegistryRegisteredHandles:
    def test_returns_handles_in_registration_order(self) -> None:
        # GIVEN a registry with three resources
        registry = _registry()
        handles = [HandleStub("A"), HandleStub("B"), HandleStub("C")]
        for h in handles:
            registry.register(h)

        # WHEN querying registered handles
        result = registry.registered_handles()

        # THEN they are returned in registration order
        assert result == handles

    def test_empty_registry_returns_empty_list(self) -> None:
        # GIVEN an empty registry
        registry = _registry()

        # WHEN querying registered handles
        result = registry.registered_handles()

        # THEN an empty list is returned
        assert result == []

    def test_deregistered_handle_not_in_list(self) -> None:
        # GIVEN a registry with a handle that has been deregistered
        registry = _registry()
        handle = HandleStub("ctrl")
        registry.register(handle)
        registry.deregister(handle)

        # WHEN querying registered handles
        result = registry.registered_handles()

        # THEN the deregistered handle is not present
        assert result == []


class TestResourceRegistryChildrenOf:
    def test_returns_children_in_registration_order(self) -> None:
        # GIVEN a parent with two children
        registry = _registry()
        parent = HandleStub("parent")
        child_a = HandleStub("child_a")
        child_b = HandleStub("child_b")
        registry.register(parent)
        registry.register(child_a, parent=parent)
        registry.register(child_b, parent=parent)

        # WHEN querying children
        result = registry.children_of(parent)

        # THEN children are returned in registration order
        assert result == [child_a, child_b]

    def test_returns_empty_for_leaf_handle(self) -> None:
        # GIVEN a handle with no children
        registry = _registry()
        handle = HandleStub("leaf")
        registry.register(handle)

        # WHEN querying children
        result = registry.children_of(handle)

        # THEN an empty list is returned
        assert result == []

    def test_returns_empty_for_unknown_handle(self) -> None:
        # GIVEN an empty registry
        registry = _registry()

        # WHEN querying children of an unregistered handle
        result = registry.children_of(HandleStub("ghost"))

        # THEN an empty list is returned
        assert result == []
