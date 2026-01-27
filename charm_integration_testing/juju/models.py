# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass


@dataclass(frozen=True)
class JujuApplicationInfo:
    charm: str
    revision: int


@dataclass(frozen=True)
class JujuIntegrationApplication:
    application: str
    endpoint: str

    def __str__(self) -> str:
        return f"{self.application}:{self.endpoint}"

    @classmethod
    def from_str(cls, value: str) -> "JujuIntegrationApplication":
        parts = value.split(":", maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"Invalid JujuIntegrationApplication string: {value}")
        application, endpoint = parts
        return cls(application=application, endpoint=endpoint)


@dataclass(frozen=True)
class JujuIntegration:
    provider: JujuIntegrationApplication
    requirer: JujuIntegrationApplication
    interface: str
