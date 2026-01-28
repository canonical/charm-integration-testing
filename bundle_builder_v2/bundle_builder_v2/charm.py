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


from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

ENDPOINT_PEERS = "peers"
ENDPOINT_REQUIRES = "requires"
ENDPOINT_PROVIDES = "provides"


class CharmChannel(BaseModel):
    model_config = ConfigDict(frozen=True)

    track: str
    risk: str
    branch: str

    @model_validator(mode="before")
    @classmethod
    # TODO(raul): remove type ignore in subsequent type checker PRs
    def validate_from_string(cls, value):  # type: ignore
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
    model_config = ConfigDict(frozen=True)

    type: str
    interface: str


class CharmConstraints(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Things to include:
    # * Endpoint is non-optional
    # * Endpoint has limit
    # * Mutual exclusion with other endpoints
    # * Endpoint must be integrated with same application as other endpoint
    # * 


class Charm(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    channel: CharmChannel
    revision: int
    ubuntu_version: str
    ubuntu_arch: str
    endpoints: dict[str, CharmEndpoint]
    priority: int = Field(default=1)
    constraints: CharmConstraints = Field(default_factory=CharmConstraints)

    def __repr__(self) -> str:
        return self.name
