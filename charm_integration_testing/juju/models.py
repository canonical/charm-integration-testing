# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from functools import total_ordering

from .handles import JujuModelHandle

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

    def parse_url(self) -> tuple[str, JujuModelHandle, str] | None:
        """Parse ``url`` (``controller:user/model.offer-name``) into owner, offering model and offer name.

        The returned ``JujuModelHandle.model`` is the bare model name (without the owner), matching
        how models are otherwise identified/compared throughout this framework (e.g. against models
        already tracked without an owner prefix). Callers that need to directly address the offering
        model (e.g. for a status query) should qualify it with the returned owner, since Juju CLI
        addressing may require it when the model's owner differs from the currently authenticated
        user. Returns None if the URL doesn't match the expected shape.
        """
        if ":" not in self.url or "/" not in self.url:
            return None
        controller, rest = self.url.split(":", 1)
        owner, model_and_offer = rest.split("/", 1)
        if "." not in model_and_offer:
            return None
        model, offer_name = model_and_offer.rsplit(".", 1)
        if not controller or not owner or not model or not offer_name:
            return None
        return owner, JujuModelHandle(controller=controller, model=model), offer_name
