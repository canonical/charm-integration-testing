# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from functools import total_ordering

_RISK_ORDER = {"stable": 0, "candidate": 1, "beta": 2, "edge": 3}


@total_ordering
@dataclass(frozen=True)
class CharmChannel:
    track: str
    risk: str
    branch: str

    @classmethod
    def parse(cls, value: str | dict[str, str]) -> "CharmChannel":
        if isinstance(value, str):
            parts = value.split("/")
            match len(parts):
                case 1:
                    return cls(track="", risk=parts[0], branch="")
                case 2:
                    return cls(track=parts[0], risk=parts[1], branch="")
                case 3:
                    return cls(track=parts[0], risk=parts[1], branch=parts[2])
                case _:
                    raise ValueError(f"Invalid channel string: {value}")
        return cls(**value)

    def __str__(self) -> str:
        return "/".join([part for part in [self.track, self.risk, self.branch] if part])

    @property
    def explicit_track(self) -> str:
        return self.track if self.track != "" else "latest"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, CharmChannel):
            return NotImplemented
        return (self.explicit_track, _RISK_ORDER.get(self.risk, 99), self.branch) < (
            other.explicit_track,
            _RISK_ORDER.get(other.risk, 99),
            other.branch,
        )


@dataclass(frozen=True)
class JujuApplicationInfo:
    charm: str
    revision: int
    channel: CharmChannel | None = None


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


@dataclass(frozen=True)
class JujuConsumedOfferInfo:
    url: str
    endpoints: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class JujuResolvedIntegration:
    """Resolved integration endpoints after CMR alias lookup.

    ``model`` is the Juju model URI (controller:model) that owns the consuming side of a CMR
    integration, or the target model for a same-model integration.
    ``endpoint_1`` and ``endpoint_2`` are the resolved application:endpoint pairs — any SAAS
    alias has already been substituted for the original application name.
    """

    model: str
    endpoint_1: JujuIntegrationApplication
    endpoint_2: JujuIntegrationApplication
