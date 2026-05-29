# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class SecretStub:
    """Stub for ops.Secret"""

    _content: dict[str, str]

    def get_content(self) -> dict[str, str]:
        return self._content


class ApplicationStub:
    """Stub for ops.Application"""


@dataclass
class RelationStub:
    """Stub for ops.Relation"""

    name: str
    id: int
    app: ApplicationStub = field(default_factory=ApplicationStub)
    data: dict[ApplicationStub, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.app not in self.data:
            self.data[self.app] = {}


@dataclass
class ModelStub:
    """Stub for ops.Model.

    secrets are kept as a field in this class so that we do not have to implement a backend"""

    _secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    requested_ids: list[str] = field(default_factory=list)
    relations: dict[str, list[RelationStub]] = field(default_factory=dict)

    def get_secret(self, id: str) -> SecretStub:  # noqa: A002
        self.requested_ids.append(id)
        return SecretStub(_content=self._secrets[id])


class RelationRoleStub(Enum):
    """Stub for ops.RelationRole"""

    peer = "peer"
    requires = "requires"
    provides = "provides"

    def is_peer(self) -> bool:
        return self is RelationRoleStub.peer


@dataclass
class RelationMetaStub:
    """Stub for ops.RelationMeta"""

    relation_name: str
    interface_name: str | None = None
    role: RelationRoleStub = field(default_factory=lambda: RelationRoleStub.requires)


@dataclass
class CharmMetaStub:
    """Stub for ops.CharmMeta"""

    relations: dict[str, RelationMetaStub] = field(default_factory=dict)


@dataclass
class CharmBaseStub:
    """Stub for ops.CharmBase"""

    meta: CharmMetaStub = field(default_factory=CharmMetaStub)
    model: ModelStub = field(default_factory=ModelStub)

    @property
    def requested_ids(self) -> list[str]:
        return self.model.requested_ids
