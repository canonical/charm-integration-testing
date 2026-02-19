# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC


class JujuExtension(ABC):
    def post_deploy(self, model: str) -> None:
        pass

    def post_scale(self, model: str) -> None:
        pass

    def post_validate(self, model: str, application: str, level: str) -> None:
        pass
