# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC


class JujuExtension(ABC):
    def post_deploy(self, model: str) -> None:
        pass

    def post_scale(self, model: str) -> None:
        pass

    def to_remove(self, model: str, *applications: str) -> None:
        pass
