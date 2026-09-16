# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC

from validators.base import PersistenceState, ValidationResult

from .handles import JujuModelHandle
from .models import PersistenceKey


class JujuExtension(ABC):
    def post_deploy(self, model: JujuModelHandle) -> None:
        pass

    def post_scale(self, model: JujuModelHandle) -> None:
        pass

    def pre_remove(self, model: JujuModelHandle, *applications: str) -> None:
        pass

    def post_validate(self, model: JujuModelHandle, application: str, level: str) -> dict[str, list[ValidationResult]]:
        return {}

    def post_persistence(
        self,
        model: JujuModelHandle,
        application: str,
        persistence: str,
        persistence_state: dict[PersistenceKey, PersistenceState],
    ) -> dict[str, list[ValidationResult]]:
        """Run a persistence lifecycle op ("prepare"/"checkpoint"/"cleanup") for *application*.

        Implementations are expected to mutate *persistence_state* in place: adding/updating
        entries for relations they seeded or verified, and removing entries once cleaned up.
        """
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
