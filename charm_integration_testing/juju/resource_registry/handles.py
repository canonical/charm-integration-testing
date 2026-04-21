# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import re
from dataclasses import dataclass

_UNSAFE_PATH_CHARS = re.compile(r"[^a-zA-Z0-9_\-]")


@dataclass(frozen=True)
class JujuControllerHandle:
    controller: str

    @property
    def resource_id(self) -> str:
        return f"juju:controller:{self.controller}"

    @property
    def resource_type(self) -> str:
        return "juju:controller"

    @property
    def path_segment(self) -> str:
        safe = _UNSAFE_PATH_CHARS.sub("-", self.controller)
        return f"juju-controller-{safe}"
