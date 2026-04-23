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

"""Shared test infrastructure for offline logic tests.

These tests use a stub CharmhubClient that serves charms from an in-memory
registry, making all tests fully offline and deterministic.
"""

from bundle_builder_x.bundle import Bundle
from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, CharmEndpointProxy, EndpointType
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.charmhub_http import CharmReleaseNotFoundException
from bundle_builder_x.constraints_dsl import parse_constraint
from bundle_builder_x.domain import (
    ApplicationConstraint,
    CrossModelIntegrationConstraint,
    IntegrationConstraint,
    ModelInit,
    initialize_global_domain,
)
from bundle_builder_x.extract import extract_solution
from bundle_builder_x.juju_version import JujuVersion

# A representative Juju version used across all logic tests.
JUJU = JujuVersion(major=3, minor=6, patch=0)


class CharmhubClientStub(CharmhubClient):
    """A drop-in stub for CharmhubClient that serves charms from an in-memory registry.

    Accepts multiple Charm objects at construction time.  When multiple charms
    share the same name (e.g. two versions on different channels), the stub
    attempts to match on track/risk first before falling back to the first
    registered entry.

    Does NOT make any network calls, so tests are fully offline.
    """

    def __init__(self, *charms: Charm) -> None:
        super().__init__()
        self._by_name: dict[str, list[Charm]] = {}
        for charm in charms:
            self._by_name.setdefault(charm.name, []).append(charm)

    def charm_from_store(
        self,
        charm_name: str,
        ubuntu_arch: str,
        juju_version: JujuVersion,
        platform: str,
        charm_track: str | None = None,
        charm_risk: str | None = None,
        charm_revision: int | None = None,
        ubuntu_version: str | None = None,
    ) -> Charm:
        candidates = self._by_name.get(charm_name, [])
        if not candidates:
            raise CharmReleaseNotFoundException(f"No charm {charm_name!r} in stub registry")

        # Prefer exact track + risk match.
        if charm_track is not None:
            for c in candidates:
                if c.channel.explicit_track == charm_track:
                    if charm_risk is None or c.channel.risk == charm_risk:
                        return c

        # Fall back to risk-only match.
        if charm_risk is not None:
            for c in candidates:
                if c.channel.risk == charm_risk:
                    return c

        # Fall back to first registered charm with that name.
        return candidates[0]

    def find_charms(
        self,
        provides: str | None = None,
        requires: str | None = None,
        platform: str | None = None,
    ) -> set[str]:
        result: set[str] = set()
        for charms in self._by_name.values():
            charm = charms[0]  # representative; interface set is same across channel variants
            for ep in charm.endpoints.values():
                if provides is not None and ep.type == EndpointType.PROVIDES and ep.interface == provides:
                    result.add(charm.name)
                    break
                if requires is not None and ep.type == EndpointType.REQUIRES and ep.interface == requires:
                    result.add(charm.name)
                    break
        return result


def make_charm(
    name: str,
    endpoints: dict[str, CharmEndpoint] | None = None,
    constraint_strs: list[str] | None = None,
    channel: str = "stable",
    proxies: list[CharmEndpointProxy] | None = None,
    priority: float = 1.0,
    revision: int = 1,
) -> Charm:
    """Build a minimal Charm suitable for use in logic tests."""
    return Charm(
        name=name,
        channel=CharmChannel.model_validate(channel),
        revision=revision,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=endpoints or {},
        constraints=[parse_constraint(c) for c in (constraint_strs or [])],
        proxies=proxies or [],
        priority=priority,
    )


def build_single_model(
    builder: BundleBuilder,
    applications: dict[str, ApplicationConstraint],
    integrations: set[IntegrationConstraint] | None = None,
    platform: str = "kubernetes",
    arch: str = "amd64",
    juju_version: JujuVersion = JUJU,
    cross_model_integrations: list[CrossModelIntegrationConstraint] | None = None,
) -> Bundle:
    """Build a single-model bundle for testing."""
    domain = initialize_global_domain(
        {
            "_default": ModelInit(
                applications=applications,
                integrations=integrations or set(),
                platform=platform,
                arch=arch,
                juju_version=juju_version,
                cross_model_integrations=list(cross_model_integrations or []),
            )
        }
    )
    z3_model = builder._solve(domain)
    solution = extract_solution(z3_model, domain, logger=builder.logger)
    return solution.bundles[0]
