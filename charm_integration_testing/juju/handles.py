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


@dataclass(frozen=True)
class JujuModelHandle:
    model: str
    controller: str | None = None

    @property
    def uri(self) -> str:
        # Bare model when controller is unknown (e.g. a Kubernetes pod hook only sees the namespace).
        if self.controller is None:
            return self.model
        return f"{self.controller}:{self.model}"

    @property
    def resource_id(self) -> str:
        return f"juju:model:{self.controller}:{self.model}"

    @property
    def resource_type(self) -> str:
        return "juju:model"

    @property
    def path_segment(self) -> str:
        safe_controller = _UNSAFE_PATH_CHARS.sub("-", self.controller or "")
        safe_model = _UNSAFE_PATH_CHARS.sub("-", self.model)
        return f"juju-model-{safe_controller}-{safe_model}"
