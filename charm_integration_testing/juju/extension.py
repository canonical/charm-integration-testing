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
        """Run after an explicit scale request or targeted unit removal."""
        pass

    def post_restart_unit(self, model: JujuModelHandle, unit: str) -> None:
        pass

    def post_delete_pod(self, model: JujuModelHandle, pod_name: str) -> None:
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

    def post_validate_excluding_units(
        self, model: JujuModelHandle, application: str, level: str, excluded_units: set[str]
    ) -> dict[str, list[ValidationResult]]:
        if excluded_units and type(self).post_validate is not JujuExtension.post_validate:
            raise NotImplementedError(
                f"{type(self).__name__} must implement post_validate_excluding_units when excluding units."
            )
        return self.post_validate(model, application, level)

    def persistence_operation(self, model: JujuModelHandle) -> str | None:
        """Return the persistence lifecycle operation for one model validation, if any."""
        return None

    def post_persistence_excluding_units(
        self,
        model: JujuModelHandle,
        application: str,
        persistence: str | None,
        excluded_units: set[str],
    ) -> dict[str, list[ValidationResult]]:
        if excluded_units and type(self).post_persistence is not JujuExtension.post_persistence:
            raise NotImplementedError(
                f"{type(self).__name__} must implement post_persistence_excluding_units when excluding units."
            )
        return self.post_persistence(model, application, persistence)

    def post_persistence(
        self, model: JujuModelHandle, application: str, persistence: str | None = None
    ) -> dict[str, list[ValidationResult]]:
        """Run the persistence lifecycle for *application*, auto-deciding the op from tracked state."""
        return {}

    def pre_bootstrap_controller(
        self, cloud: str, controller: str, bootstrap_configuration: dict[str, str]
    ) -> dict[str, str]:
        """Return the bootstrap configuration to create *controller* with; extensions may extend it."""
        return bootstrap_configuration

    def post_bootstrap_controller(self, controller: str) -> None:
        pass

    def pre_add_model(
        self, controller: str, model: str, model_config: dict[str, str], cloud: str | None
    ) -> dict[str, str]:
        """Return the model configuration to create *model* with; extensions may extend *model_config*."""
        return model_config

    def post_add_model(self, controller: str, model: str) -> None:
        """Notify the extension that *model* was added."""
        pass

    def post_add_model_with_cloud(self, controller: str, model: str, cloud: str | None) -> None:
        """Notify the extension that *model* was added on *cloud*."""
        self.post_add_model(controller, model)

    def pre_kill_controller(self, controller: str) -> None:
        pass

    def post_kill_controller(self, controller: str) -> None:
        pass

    def post_migrate_model(self, model: str, source: str, target: str) -> None:
        pass
