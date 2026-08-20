# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from __future__ import annotations

from typing import TYPE_CHECKING

from resource_registry import ResourceRegistry

from juju import JujuControllerHandle, JujuModelHandle
from juju.extension import JujuExtension

if TYPE_CHECKING:
    from juju.backend import JujuBackend


class JujuResourceRegistryExtension(JujuExtension):
    _backend: JujuBackend
    _registry: ResourceRegistry

    def __init__(
        self,
        backend: JujuBackend,
        registry: ResourceRegistry,
    ) -> None:
        self._backend = backend
        self._registry = registry

    def post_bootstrap_controller(self, controller: str) -> None:
        handle = JujuControllerHandle(controller=controller)
        self._registry.register(
            handle=handle,
            destroyer=lambda: self._backend.kill_controller(controller=controller),
        )

    def post_add_model(self, controller: str, model: str) -> None:
        controller_handle = JujuControllerHandle(controller=controller)
        if not self._registry.is_registered(controller_handle):
            # Controller pre-existed this session: register it with no destroyer so logs
            # are still collected but teardown is a no-op.
            self._registry.register(handle=controller_handle, destroyer=None)
        model_handle = JujuModelHandle(controller=controller, model=model)
        self._registry.register(
            handle=model_handle,
            parent=controller_handle,
        )

    def pre_kill_controller(self, controller: str) -> None:
        controller_handle = JujuControllerHandle(controller=controller)
        # Tear down child models (collect logs + deregister) before collecting controller logs.
        for child_handle in self._registry.children_of(controller_handle):
            self._registry.teardown(child_handle)
        self._registry.collect_logs(controller_handle)

    def post_kill_controller(self, controller: str) -> None:
        handle = JujuControllerHandle(controller=controller)
        self._registry.deregister(handle)

    def post_migrate_model(self, model: str, source: str, target: str) -> None:
        source_handle = JujuModelHandle(controller=source, model=model)
        if self._registry.is_registered(source_handle):
            self._registry.deregister(source_handle)
        target_controller_handle = JujuControllerHandle(controller=target)
        if not self._registry.is_registered(target_controller_handle):
            self._registry.register(handle=target_controller_handle, destroyer=None)
        self._registry.register(
            handle=JujuModelHandle(controller=target, model=model),
            parent=target_controller_handle,
        )
