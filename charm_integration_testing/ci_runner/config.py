# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import platform
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator


def host_arch() -> str:
    arch = platform.machine()
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(arch, arch)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str = Field(min_length=1)
    neighbor: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    neighbor_endpoint: str = Field(min_length=1)
    pytest_args: list[str] = Field(default_factory=list)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arch: str = Field(default_factory=host_arch)
    base: str | None = None
    channel: str = "latest/stable"
    resources: dict[str, str] = Field(default_factory=dict)
    endpoints: list[str] | None = None
    neighbors: list[str] | None = None
    scenarios: list[Scenario] | None = None
    pytest_args: list[str] = Field(default_factory=list)
    overrides: Path | None = None
    observer_execution_id: PositiveInt | None = None

    @field_validator("endpoints", "neighbors", "scenarios")
    @classmethod
    def nonempty_selection(cls, value: list[str] | list[Scenario] | None) -> list[str] | list[Scenario] | None:
        if value == []:
            raise ValueError("Use null or omit the selection for discovery, not an empty list")
        return value


def load_config(path: Path | None) -> Config:
    if path is None:
        return Config()
    path = path.resolve(strict=True)
    config = Config.model_validate_json(path.read_text(encoding="utf-8"))
    if config.overrides is not None:
        config.overrides = (path.parent / config.overrides).resolve(strict=True)
    config.resources = {
        name: str((path.parent / value).resolve()) if (path.parent / value).is_file() else value
        for name, value in config.resources.items()
    }
    return config
