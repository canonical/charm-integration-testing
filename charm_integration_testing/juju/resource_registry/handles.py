# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass


@dataclass(frozen=True)
class JujuControllerHandle:
    controller: str
    model: str | None = None

    @property
    def resource_id(self) -> str:
        return f"juju:controller:{self.controller}"

    @property
    def resource_type(self) -> str:
        return "juju:controller"

    @property
    def path_segment(self) -> str:
        return f"juju-controller-{self.controller}"
