# Copyright (C) 2025 Canonical Ltd

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

from pydantic import Field

from .immutable_dataclass import cached_method, immutable_dataclass

ENDPOINT_PEERS = "peers"
ENDPOINT_REQUIRES = "requires"
ENDPOINT_PROVIDES = "provides"


@immutable_dataclass
class CharmEndpointOptionality:
    all_of: frozenset["CharmEndpointOptionality"] | None = None
    any_of: frozenset["CharmEndpointOptionality"] | None = None
    none_of: frozenset["CharmEndpointOptionality"] | None = None
    endpoint_integrated: str | None = None

    @cached_method
    def is_optional(self, integrated_endpoints: frozenset[str]) -> bool:
        return all(
            [
                # all of
                all(condition.is_optional(integrated_endpoints) for condition in self.all_of)
                if self.all_of is not None
                else True,
                # any of
                any(condition.is_optional(integrated_endpoints) for condition in self.any_of)
                if self.any_of is not None
                else True,
                # none of
                all(not condition.is_optional(integrated_endpoints) for condition in self.none_of)
                if self.none_of is not None
                else True,
                # endpoint integrated
                (self.endpoint_integrated in integrated_endpoints) if self.endpoint_integrated is not None else True,
            ]
        )

    @classmethod
    def from_bool(cls, value: bool) -> "CharmEndpointOptionality":
        if value:
            return cls(all_of=frozenset())
        else:
            return cls(any_of=frozenset())


@immutable_dataclass
class CharmEndpoint:
    type: str
    name: str
    interface: str
    optionality: CharmEndpointOptionality
    limit: int | None


CharmConfig = tuple[tuple[str, str | int], ...]


@immutable_dataclass
class Charm:
    name: str
    channel: str
    revision: int
    ubuntu_version: str
    ubuntu_arch: str
    endpoints: frozenset[CharmEndpoint]
    priority: float  # greater priority values mean a node with this charm is prioritized
    test_configs: tuple[CharmConfig, ...] = Field(default_factory=tuple)

    def __repr__(self) -> str:
        return self.name
