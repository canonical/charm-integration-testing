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
    """Identifies a model by controller and bare (unqualified) model name.

    ``owner`` is optional and only needed to directly address a model whose owner differs from
    the currently authenticated user (e.g. a cross-model relation's offering model, via ``uri``).
    It's not part of the model's identity for comparison/tracking purposes elsewhere in this
    framework, where models are otherwise identified by their bare name alone.
    """

    model: str
    controller: str
    owner: str | None = None

    @property
    def uri(self) -> str:
        qualified_model = f"{self.owner}/{self.model}" if self.owner is not None else self.model
        return f"{self.controller}:{qualified_model}"

    @property
    def resource_id(self) -> str:
        return f"juju:model:{self.controller}:{self.model}"

    @property
    def resource_type(self) -> str:
        return "juju:model"

    @property
    def path_segment(self) -> str:
        safe_controller = _UNSAFE_PATH_CHARS.sub("-", self.controller)
        safe_model = _UNSAFE_PATH_CHARS.sub("-", self.model)
        return f"juju-model-{safe_controller}-{safe_model}"
