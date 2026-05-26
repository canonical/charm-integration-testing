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

from validators.base import ValidationRole


class AppStub:
    """Minimal stand-in for ops.Application.  Must be hashable (dict key)."""


@dataclass
class RelationRoleStub:
    """Stub for relation role, commonly used across validator tests."""

    name: ValidationRole


@dataclass
class RelationStub:
    name: str
    app: AppStub | None = None
    data: dict[AppStub | None, dict[str, str]] = field(default_factory=dict)
    id: int = 0


@dataclass
class RelationMetaStub:
    interface_name: str | None = None
    role: RelationRoleStub = field(default_factory=lambda: RelationRoleStub(name="requires"))


@dataclass
class SecretStub:
    content: dict[str, str]

    def get_content(self) -> dict[str, str]:
        return self.content


@dataclass
class ModelStub:
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    requested_ids: list[str] = field(default_factory=list)

    def get_secret(self, id: str) -> SecretStub:  # noqa: A002
        self.requested_ids.append(id)
        return SecretStub(content=self.secrets[id])


@dataclass
class CharmMetaStub:
    relations: dict[str, RelationMetaStub] = field(default_factory=dict)


@dataclass
class CharmStub:
    relation_name: str = "test-relation"
    meta: CharmMetaStub = field(default_factory=CharmMetaStub)
    interface_name: str = "test-interface"
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.model = ModelStub(secrets=self.secrets)
        self.meta = CharmMetaStub(
            relations={
                self.relation_name: RelationMetaStub(
                    interface_name=self.interface_name, role=RelationRoleStub("requires")
                )
            }
        )
