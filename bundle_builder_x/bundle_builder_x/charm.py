# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

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
        return not self.unsatisfied_requirements(juju_version, features)

    def describe(self) -> str:
        """Return a deterministic representation of this assumes expression."""
        if self.all_of is not None:
            return "all-of(" + ",".join(sorted(entry.describe() for entry in self.all_of)) + ")"
        if self.any_of is not None:
            return "any-of(" + ",".join(sorted(entry.describe() for entry in self.any_of)) + ")"
        if self.op is not None and self.required_version is not None:
            return f"juju{self.op}{self.required_version}"
        if self.feature is not None:
            return f"feature={self.feature}"
        return "empty"

    def unsatisfied_requirements(
        self,
        juju_version: JujuVersion | None,
        features: frozenset[str] = frozenset(),
    ) -> tuple[str, ...]:
        """Explain which requirements are not satisfied by an environment."""
        failures: list[str] = []
        if self.all_of is not None:
            for entry in self.all_of:
                failures.extend(entry.unsatisfied_requirements(juju_version, features))
        if self.any_of is not None and not any(entry.satisfied_by(juju_version, features) for entry in self.any_of):
            failures.append(self.describe())
        if (
            self.op is not None
            and self.required_version is not None
            and juju_version is not None
            and not ASSUMES_OPS[self.op](juju_version, self.required_version)
        ):
            failures.append(self.describe())
        if self.feature is not None and self.feature not in features:
            failures.append(self.describe())
        return tuple(sorted(set(failures)))


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
    # Whether the remove-and-restore test may tear down this endpoint's integration. Charmhub has
    # no native concept of this today, so it is always override-sourced (see CharmEndpointOverrides).
    removable: bool = Field(default=True)


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
    # Platforms (e.g. "machine", "kubernetes") this charm is known to support. Platform
    # overrides win when present; otherwise this falls back to the charm's own metadata
    # (a non-empty `containers` block means "kubernetes", its absence means "machine").
    # No default is provided: every Charm must state which platform(s) it supports.
    platforms: list[str]

    def __repr__(self) -> str:
        return self.name
