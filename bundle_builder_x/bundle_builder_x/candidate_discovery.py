# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections.abc import Iterator
from dataclasses import dataclass

from .charm import Charm, CharmChannel
from .charmhub import CharmhubClient
from .domain import DomainModel, ModelRef
from .release_errors import CharmReleaseNotFoundException


@dataclass(frozen=True)
class ReleaseRequirements:
    track: str | None = None
    risk: str | None = None
    branch: str | None = None
    revision: int | None = None
    base: str | None = None

    def combine(self, other: "ReleaseRequirements") -> "ReleaseRequirements | None":
        """Intersect selectors, returning None when they conflict."""
        pairs = (
            (self.track, other.track),
            (self.risk, other.risk),
            (self.branch, other.branch),
            (self.revision, other.revision),
            (self.base, other.base),
        )
        if any(left is not None and right is not None and left != right for left, right in pairs):
            return None
        return ReleaseRequirements(
            track=self.track if self.track is not None else other.track,
            risk=self.risk if self.risk is not None else other.risk,
            branch=self.branch if self.branch is not None else other.branch,
            revision=self.revision if self.revision is not None else other.revision,
            base=self.base if self.base is not None else other.base,
        )

    @property
    def specificity(self) -> int:
        return sum(value is not None for value in (self.track, self.risk, self.branch, self.revision, self.base))

    @classmethod
    def channel(cls, channel: CharmChannel) -> "ReleaseRequirements":
        return cls(track=channel.explicit_track, risk=channel.risk or None, branch=channel.branch)


@dataclass(frozen=True)
class DependencyAlternative:
    model: ModelRef
    anchor_id: int
    endpoint: str
    target_id: int


class CandidateDiscovery:
    """Build-scoped release discovery; solver hints never override application pins."""

    def __init__(self, client: CharmhubClient) -> None:
        self.client = client
        self.requirements: dict[DependencyAlternative, list[ReleaseRequirements]] = {}
        self._channels: dict[str, list[CharmChannel]] = {}
        self._releases: dict[tuple[str, ModelRef, ReleaseRequirements], Charm | None] = {}

    def candidates(
        self,
        alternative: DependencyAlternative,
        model: DomainModel,
        current: Charm,
        required: ReleaseRequirements,
        pins: list[ReleaseRequirements],
        *,
        widen: bool = True,
    ) -> Iterator[Charm]:
        learned = self.requirements.setdefault(alternative, [])
        # Hints can describe alternatives, not a conjunction. Prefer compatible
        # combinations but retain individual searches for the solver to arbitrate.
        combined = [merged for old in learned if (merged := old.combine(required)) is not None]
        for learned_requirement in [*combined, required]:
            if learned_requirement not in learned:
                learned.append(learned_requirement)
        searches = sorted(learned, key=lambda requirement: requirement.specificity, reverse=True)
        for hint in searches:
            for pin in pins:
                requirement = hint.combine(pin)
                if requirement is None:
                    continue
                for request in self._requests(current, requirement, widen=widen):
                    charm = self._fetch_release(current.name, alternative.model, model, request)
                    if charm is not None:
                        yield charm

    def _fetch_release(
        self, name: str, model_ref: ModelRef, model: DomainModel, request: ReleaseRequirements
    ) -> Charm | None:
        """Cache both successful and unavailable lookups for this build."""
        key = (name, model_ref, request)
        if key not in self._releases:
            try:
                self._releases[key] = self.client.charm_from_store(
                    charm_name=name,
                    ubuntu_arch=model.arch,
                    juju_version=model.juju_version,
                    platform=model.platform,
                    charm_track=request.track,
                    charm_risk=request.risk,
                    charm_branch=request.branch,
                    charm_revision=request.revision,
                    ubuntu_version=request.base,
                )
            except CharmReleaseNotFoundException:
                self._releases[key] = None
        return self._releases[key]

    def _requests(
        self, current: Charm, requirement: ReleaseRequirements, *, widen: bool
    ) -> Iterator[ReleaseRequirements]:
        # Retain default dimensions only where the request leaves a choice.
        preferred = ReleaseRequirements(
            track=requirement.track if requirement.track is not None else current.channel.explicit_track,
            risk=requirement.risk if requirement.risk is not None else current.channel.risk or None,
            branch=requirement.branch if requirement.branch is not None else current.channel.branch,
            revision=requirement.revision,
            base=requirement.base,
        )
        yield preferred
        if not widen:
            return
        # A revision may no longer be a channel head. Keep the existing revision
        # lookup available even when the channel map contains no matching release.
        fixed_channel = requirement.track is not None and requirement.risk is not None
        if (requirement.revision is not None or fixed_channel) and requirement != preferred:
            yield requirement
        if fixed_channel:
            return
        if current.name not in self._channels:
            self._channels[current.name] = self.client.get_charm_channels(current.name)
        for channel in sorted(
            self._channels[current.name],
            key=lambda channel: (channel.explicit_track != preferred.track, channel),
        ):
            # Branch releases are opt-in, as in the client's ordinary channel lookups.
            if channel.branch and channel.branch != requirement.branch:
                continue
            request = requirement.combine(ReleaseRequirements.channel(channel))
            if request is not None and request != preferred and request != requirement:
                yield request
