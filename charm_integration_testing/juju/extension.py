# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC

from validators.base import ValidationResult


class JujuExtension(ABC):
    def post_deploy(self, model: str) -> None:
        pass

    def post_scale(self, model: str) -> None:
        pass

    def pre_remove(self, model: str, *applications: str) -> None:
        pass

    def post_validate(self, model: str, application: str, level: str) -> dict[str, list[ValidationResult]]:
        return {}

    def post_bootstrap_controller(self, controller: str) -> None:
        pass

    def pre_kill_controller(self, controller: str) -> None:
        pass

    def post_migrate_model(self, model: str, source: str, target: str) -> None:
        pass
