# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC


class JujuExtension(ABC):
    def post_deploy(self, model: str):
        pass

    def post_scale(self, model: str):
        pass
