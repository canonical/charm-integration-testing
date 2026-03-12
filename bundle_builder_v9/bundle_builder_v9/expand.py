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

"""Charm pool expansion: given missing-relation signals, find new charms to add."""

import logging

from .charm import Charm, EndpointType
from .charmhub import CharmhubClient
from .exceptions import is_missing_relation


def expand(
    signals: list,
    charms: dict[str, Charm],
    charmhub_client: CharmhubClient,
    platform: str,
    arch: str,
    logger: logging.Logger = logging.getLogger(__name__),
) -> list[str]:
    """Return a list of new charm names that can satisfy the missing-relation signals.

    One candidate is picked per unique (app, endpoint) signal. Already-present
    charms and charms already being added in this batch are skipped.
    """
    current_charm_names = {charm.name for charm in charms.values()}
    provided_interfaces = {
        ep.interface
        for charm in charms.values()
        for ep in charm.endpoints.values()
        if ep.type == EndpointType.PROVIDES
    }
    required_interfaces = {
        ep.interface
        for charm in charms.values()
        for ep in charm.endpoints.values()
        if ep.type == EndpointType.REQUIRES
    }

    new_charm_names: list[str] = []
    already_adding: set[str] = set()

    for signal in signals:
        if not is_missing_relation(signal):
            continue

        charm = charms.get(signal.app)
        if charm is None:
            continue
        ep = charm.endpoints.get(signal.endpoint)
        if ep is None:
            logger.debug(f"Unknown endpoint '{signal.endpoint}' on '{signal.app}', skipping")
            continue

        interface = ep.interface
        # Find a charm that fills the complementary side of this endpoint.
        if ep.type == EndpointType.REQUIRES:
            candidates = charmhub_client.find_charms(provides=interface, platform=platform)
        else:
            candidates = charmhub_client.find_charms(requires=interface, platform=platform)

        best = _pick_best(
            candidates,
            exclude=current_charm_names | already_adding,
            required_interfaces=required_interfaces,
            provided_interfaces=provided_interfaces,
            arch=arch,
            charmhub_client=charmhub_client,
            logger=logger,
        )
        if best is None:
            logger.warning(f"No candidate found for '{signal.app}:{signal.endpoint}' (interface '{interface}')")
            continue

        logger.info(f"Expanding: adding '{best}' to satisfy '{signal.app}:{signal.endpoint}'")
        new_charm_names.append(best)
        already_adding.add(best)

    return new_charm_names


def _pick_best(
    candidates: set[str],
    exclude: set[str],
    required_interfaces: set[str],
    provided_interfaces: set[str],
    arch: str,
    charmhub_client: CharmhubClient,
    logger: logging.Logger,
) -> str | None:
    """Return the highest-scoring candidate charm name, or None if none qualify."""
    best_name: str | None = None
    best_score: int = -(10**9)

    for charm_name in candidates:
        if charm_name in exclude:
            continue
        try:
            charm = charmhub_client.charm_from_store(charm_name=charm_name, ubuntu_arch=arch)
        except Exception:
            continue
        score = _score(charm, required_interfaces, provided_interfaces)
        logger.debug(f"  Candidate '{charm_name}': score={score}")
        if score > best_score:
            best_score = score
            best_name = charm_name

    return best_name


def _score(charm: Charm, required_interfaces: set[str], provided_interfaces: set[str]) -> int:
    """Score a candidate charm.

    Higher is better:
      +1 for each interface it provides that the pool already needs.
      -1 for each new interface it requires that the pool doesn't yet provide.
    """
    satisfies = sum(
        1
        for ep in charm.endpoints.values()
        if ep.type == EndpointType.PROVIDES and ep.interface in required_interfaces
    )
    adds_requirements = sum(
        1
        for ep in charm.endpoints.values()
        if ep.type == EndpointType.REQUIRES and ep.interface not in provided_interfaces
    )
    return satisfies - adds_requirements
