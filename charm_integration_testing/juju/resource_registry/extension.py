# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from resource_registry import ResourceRegistry

from juju.backend import JujuBackend
from juju.extension import JujuExtension

from .handles import JujuControllerHandle


class JujuResourceRegistryExtension(JujuExtension):
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
            destroyer=lambda: self._backend.kill_controller(controller),
        )

    def pre_kill_controller(self, controller: str) -> None:
        handle = JujuControllerHandle(controller=controller)
        self._registry.collect_logs(handle)

    def post_kill_controller(self, controller: str) -> None:
        handle = JujuControllerHandle(controller=controller)
        self._registry.deregister(handle)
