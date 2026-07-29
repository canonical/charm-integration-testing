# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

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


class UnitStub:
    """Stub for ops.Unit.

    A plain class (not dataclass) so that instances are hashable by identity,
    matching the behaviour of real ops.Unit objects used as relation.data keys."""

    def __init__(self, name: str = "unit/0") -> None:
        self.name = name


@dataclass
class RelationStub:
    """Stub for ops.Relation"""

    name: str
    id: int
    app: ApplicationStub | None = field(default_factory=ApplicationStub)
    data: dict[ApplicationStub | UnitStub | None, dict[str, str]] = field(default_factory=dict)
    units: frozenset[UnitStub] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.app is not None and self.app not in self.data:
            self.data[self.app] = {}


@dataclass
class ModelStub:
    """Stub for ops.Model.

    Secrets are kept as a field in this class so that we do not have to implement a backend."""

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
    unit: UnitStub = field(default_factory=lambda: UnitStub("app/0"))

    @property
    def requested_ids(self) -> list[str]:
        return self.model.requested_ids
