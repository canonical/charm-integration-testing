# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC

from validators.base import ValidationResult

from .handles import JujuModelHandle
from .models import JujuIntegrationApplication


class JujuExtension(ABC):
    def post_deploy(self, model: JujuModelHandle) -> None:
        pass

    def post_scale(self, model: JujuModelHandle) -> None:
        pass

    def pre_remove(self, model: JujuModelHandle, *applications: str) -> None:
        pass

    def pre_remove_integration(
        self,
        model: JujuModelHandle,
        endpoint_1: JujuIntegrationApplication,
        endpoint_2: JujuIntegrationApplication,
    ) -> None:
        pass

    def post_validate(self, model: JujuModelHandle, application: str, level: str) -> dict[str, list[ValidationResult]]:
        return {}

    def post_persistence(self, model: JujuModelHandle, application: str) -> dict[str, list[ValidationResult]]:
        """Run the persistence lifecycle for *application*, auto-deciding the op from tracked state."""
        return {}

    def post_bootstrap_controller(self, controller: str) -> None:
        pass

    def post_add_model(self, controller: str, model: str) -> None:
        pass

    def pre_kill_controller(self, controller: str) -> None:
        pass

    def post_kill_controller(self, controller: str) -> None:
        pass

    def post_migrate_model(self, model: str, source: str, target: str) -> None:
        pass
