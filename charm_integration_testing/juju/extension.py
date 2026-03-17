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
