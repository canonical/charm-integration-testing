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

import operator
from enum import Enum
from functools import total_ordering
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from .constraints_dsl import AnyExpr
from .juju_version import JujuVersion

ASSUMES_OPS: dict[str, Callable[["JujuVersion", "JujuVersion"], bool]] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
}

# The juju-info interface is implicitly provided by every machine charm (mirrors Juju's
# state/application.go Endpoints()). It is not declared in charm metadata for most principals,
# so charmhub's find API returns no results when filtering by provides=juju-info.
JUJU_INFO_INTERFACE = "juju-info"


class CharmAssumesEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    all_of: frozenset["CharmAssumesEntry"] | None = None
    any_of: frozenset["CharmAssumesEntry"] | None = None
    op: str | None = None
    required_version: JujuVersion | None = None
    feature: str | None = None

    @field_validator("op")
    @classmethod
    def _validate_op(cls, v: str | None) -> str | None:
        if v is not None and v not in ASSUMES_OPS:
            raise ValueError(f"Unknown juju version operator {v!r}. Expected one of: {list(ASSUMES_OPS)}")
        return v

    def satisfied_by(self, juju_version: JujuVersion | None, features: frozenset[str] = frozenset()) -> bool:
        return all(
            [
                # all of
                all(entry.satisfied_by(juju_version, features) for entry in self.all_of)
                if self.all_of is not None
                else True,
                # any of
                any(entry.satisfied_by(juju_version, features) for entry in self.any_of)
                if self.any_of is not None
                else True,
                # juju version constraint (skipped when juju_version is unknown)
                ASSUMES_OPS[self.op](juju_version, self.required_version)
                if self.op is not None and self.required_version is not None and juju_version is not None
                else True,
                # feature requirement
                self.feature in features if self.feature is not None else True,
            ]
        )


class EndpointType(str, Enum):
    PEERS = "peers"
    REQUIRES = "requires"
    PROVIDES = "provides"


class EndpointScope(str, Enum):
    CONTAINER = "container"
    GLOBAL = "global"


@total_ordering
class CharmChannel(BaseModel):
    model_config = ConfigDict(frozen=True)

    track: str
    risk: str
    branch: str

    @model_validator(mode="before")
    @classmethod
    def validate_from_string(cls, value: str | dict[str, str]) -> dict[str, str]:
        if isinstance(value, str):
            parts = value.split("/")
            match len(parts):
                case 1:
                    return {"track": "", "risk": parts[0], "branch": ""}
                case 2:
                    return {"track": parts[0], "risk": parts[1], "branch": ""}
                case 3:
                    return {"track": parts[0], "risk": parts[1], "branch": parts[2]}
                case _:
                    raise ValueError(f"Invalid channel string: {value}")
        return value

    @model_serializer(mode="plain")
    def serialize_model(self) -> str:
        return str(self)

    def __str__(self) -> str:
        return "/".join([part for part in [self.track, self.risk, self.branch] if part])

    @property
    def explicit_track(self) -> str:
        return self.track if self.track != "" else "latest"

    def __lt__(self, other: "CharmChannel") -> bool:
        _risk_order = {"stable": 0, "candidate": 1, "beta": 2, "edge": 3}
        return (self.explicit_track, _risk_order.get(self.risk, 99), self.branch) < (
            other.explicit_track,
            _risk_order.get(other.risk, 99),
            other.branch,
        )


class CharmEndpoint(BaseModel):
    type: EndpointType
    interface: str
    optional: bool = Field(default=False)
    limit: int | None = Field(default=None)
    scope: EndpointScope | None = Field(default=None)
    cyclic: bool = Field(default=False)
    features: frozenset[str] = Field(default_factory=frozenset)


class CharmEndpointProxy(BaseModel):
    interface: str
    requires: str
    provides: str


CharmConfigValue = str | int | float | bool | None

CharmResourceValue = str | None


class Charm(BaseModel):
    name: str
    channel: CharmChannel
    revision: int
    ubuntu_version: str
    ubuntu_arch: str
    subordinate: bool = False
    endpoints: dict[str, CharmEndpoint]
    proxies: list[CharmEndpointProxy] = Field(default_factory=list)
    priority: float = Field(default=1)
    configs: dict[str, list[CharmConfigValue]] = Field(default_factory=dict)
    config_defaults: dict[str, CharmConfigValue] = Field(default_factory=dict)
    resources: dict[str, list[CharmResourceValue]] = Field(default_factory=dict)
    assumes: CharmAssumesEntry = Field(default_factory=CharmAssumesEntry)
    constraints: list[AnyExpr] = Field(default_factory=list)

    def __repr__(self) -> str:
        return self.name
