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


from pydantic import Field, model_serializer, model_validator

from .immutable_dataclass import cached_method, immutable_dataclass

ENDPOINT_PEERS = "peers"
ENDPOINT_REQUIRES = "requires"
ENDPOINT_PROVIDES = "provides"


@immutable_dataclass
class CharmChannel:
    track: str
    risk: str
    branch: str

    @model_validator(mode="before")
    @classmethod
    def validate_from_string(cls, value):
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
        """Create a CharmEndpointOptionality from a boolean value.

        Args:
            value: If True, creates an always-optional endpoint (satisfied by empty all_of).
                   If False, creates a never-optional endpoint (unsatisfied by empty any_of).

        Returns:
            CharmEndpointOptionality instance representing the boolean optionality.
        """
        if value:
            return cls(all_of=frozenset())
        else:
            return cls(any_of=frozenset())


@immutable_dataclass
class CharmLimitCriteria:
    all_of: frozenset["CharmLimitCriteria"] | None = None
    any_of: frozenset["CharmLimitCriteria"] | None = None
    none_of: frozenset["CharmLimitCriteria"] | None = None
    endpoint_integrated: str | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_config_from_dict(cls, value):
        if isinstance(value, list):
            return {"all_of": value}
        return value

    @cached_method
    def valid(
        self,
        integrated_endpoints: set[str],
    ) -> bool:
        return all(
            [
                # all of
                all(condition.valid(integrated_endpoints) for condition in self.all_of)
                if self.all_of is not None
                else True,
                # any of
                any(condition.valid(integrated_endpoints) for condition in self.any_of)
                if self.any_of is not None
                else True,
                # none of
                all(not condition.valid(integrated_endpoints) for condition in self.none_of)
                if self.none_of is not None
                else True,
                # endpoint integrated
                (self.endpoint_integrated in integrated_endpoints) if self.endpoint_integrated is not None else True,
            ]
        )

    @classmethod
    def from_bool(cls, value: bool) -> "CharmLimitCriteria":
        """Create a CharmLimitCriteria from a boolean value.

        Args:
            value: If True, creates criteria that's always valid (satisfied by empty all_of).
                   If False, creates criteria that's never valid (unsatisfied by empty any_of).

        Returns:
            CharmLimitCriteria instance representing the boolean validity.
        """
        if value:
            return cls(all_of=frozenset())
        else:
            return cls(any_of=frozenset())


@immutable_dataclass
class CharmLimit:
    criteria: CharmLimitCriteria = Field(default=CharmLimitCriteria.from_bool(True))
    limit: int | None = None


@immutable_dataclass
class CharmEndpoint:
    type: str
    name: str
    interface: str
    optionality: CharmEndpointOptionality
    limits: tuple[CharmLimit, ...]

    @cached_method
    def limit(self, integrated_endpoints: frozenset[str]) -> int | None:
        for limit in self.limits:
            if limit.criteria.valid(integrated_endpoints):
                return limit.limit
        return None


CharmConfig = tuple[tuple[str, str | int], ...]


@immutable_dataclass
class CharmConfigCriteria:
    all_of: frozenset["CharmConfigCriteria"] | None = None
    any_of: frozenset["CharmConfigCriteria"] | None = None
    none_of: frozenset["CharmConfigCriteria"] | None = None
    track: str | None = None
    endpoint_integrated: str | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_config_from_dict(cls, value):
        if isinstance(value, list):
            return {"all_of": value}
        return value

    @cached_method
    def valid(
        self,
        channel: CharmChannel,
        integrated_endpoints: set[str],
    ) -> bool:
        return all(
            [
                # all of
                all(condition.valid(channel, integrated_endpoints) for condition in self.all_of)
                if self.all_of is not None
                else True,
                # any of
                any(condition.valid(channel, integrated_endpoints) for condition in self.any_of)
                if self.any_of is not None
                else True,
                # none of
                all(not condition.valid(channel, integrated_endpoints) for condition in self.none_of)
                if self.none_of is not None
                else True,
                # charm track
                (channel.explicit_track == self.track) if self.track is not None else True,
                # endpoint integrated
                (self.endpoint_integrated in integrated_endpoints) if self.endpoint_integrated is not None else True,
            ]
        )

    @classmethod
    def from_bool(cls, value: bool) -> "CharmConfigCriteria":
        """Create a CharmConfigCriteria from a boolean value.

        Args:
            value: If True, creates criteria that's always valid (satisfied by empty all_of).
                   If False, creates criteria that's never valid (unsatisfied by empty any_of).

        Returns:
            CharmConfigCriteria instance representing the boolean validity.
        """
        if value:
            return cls(all_of=frozenset())
        else:
            return cls(any_of=frozenset())


@immutable_dataclass
class CharmTestConfig:
    criteria: CharmConfigCriteria = Field(default=CharmConfigCriteria.from_bool(True))
    config: CharmConfig = Field(default_factory=CharmConfig)

    @model_validator(mode="before")
    @classmethod
    def validate_config_from_dict(cls, value):
        if isinstance(value, dict) and "config" in value:
            if isinstance(value["config"], dict):
                # Convert dict to tuple of tuples
                value = value.copy()
                value["config"] = tuple(sorted(value["config"].items()))
        return value


@immutable_dataclass
class Charm:
    name: str
    channel: CharmChannel
    revision: int
    ubuntu_version: str
    ubuntu_arch: str
    endpoints: frozenset[CharmEndpoint]
    priority: float  # greater priority values mean a node with this charm is prioritized
    test_configs: tuple[CharmTestConfig, ...] = Field(default_factory=tuple)

    def __repr__(self):
        return self.name
