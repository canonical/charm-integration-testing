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

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class EndpointType(str, Enum):
    PEERS = "peers"
    REQUIRES = "requires"
    PROVIDES = "provides"


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


class CharmEndpoint(BaseModel):
    type: EndpointType
    interface: str
    optional: bool = Field(default=False)
    limit: int | None = Field(default=None)


CharmConfig = dict[str, str | int | float | bool | None]


class Charm(BaseModel):
    name: str
    channel: CharmChannel
    revision: int
    ubuntu_version: str
    ubuntu_arch: str
    endpoints: dict[str, CharmEndpoint]
    priority: float = Field(default=1)
    configs: list[CharmConfig] = Field(default_factory=list)
    ruleset_url: str | None = None

    def __repr__(self) -> str:
        return self.name
