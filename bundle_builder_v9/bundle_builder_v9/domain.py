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

"""Hypothesis search-space construction for bundle building."""

from hypothesis import strategies as st

from .bundle import Application, ApplicationEndpoint, Bundle, Integration
from .charm import Charm, EndpointType


def initialize_domain(
    charms: dict[str, Charm],
    required_integrations: list[Integration],
    platform: str,
    arch: str,
) -> "st.SearchStrategy[Bundle]":
    """Build a Hypothesis strategy over all valid Bundles for the given charm pool.

    Args:
        charms: mapping of application-name → Charm for the current pool.
        required_integrations: integrations that must always be present.
        platform: deployment platform (kubernetes or machine).
        arch: target architecture.
    """
    required_set = set(required_integrations)

    # Discover all compatible optional integrations from charm endpoint metadata.
    # Two endpoints are compatible when they share an interface and are REQUIRES↔PROVIDES.
    optional_integrations: list[Integration] = []
    app_names = list(charms.keys())
    for i, app1 in enumerate(app_names):
        charm1 = charms[app1]
        for app2 in app_names[i + 1 :]:
            charm2 = charms[app2]
            for ep1_name, ep1 in charm1.endpoints.items():
                for ep2_name, ep2 in charm2.endpoints.items():
                    if ep1.interface != ep2.interface:
                        continue
                    if (ep1.type == EndpointType.REQUIRES and ep2.type == EndpointType.PROVIDES) or (
                        ep1.type == EndpointType.PROVIDES and ep2.type == EndpointType.REQUIRES
                    ):
                        candidate = Integration.create(
                            ApplicationEndpoint(application=app1, endpoint=ep1_name),
                            ApplicationEndpoint(application=app2, endpoint=ep2_name),
                        )
                        if candidate not in required_set:
                            optional_integrations.append(candidate)

    # Strategy for configs: sample one config per application (or empty dict if none available).
    config_strategy = st.fixed_dictionaries({
        app_name: st.sampled_from(charm.configs) if charm.configs else st.just({})
        for app_name, charm in charms.items()
    })

    # Strategy for relations: required integrations are always included;
    # a random unique subset of optional ones is added on top.
    if optional_integrations:
        relation_strategy: st.SearchStrategy[list[Integration]] = st.lists(
            st.sampled_from(optional_integrations),
            unique=True,
        ).map(lambda optional: required_integrations + optional)
    else:
        relation_strategy = st.just(required_integrations)

    return st.fixed_dictionaries({
        "configs": config_strategy,
        "relations": relation_strategy,
    }).map(
        lambda d: Bundle(
            applications={
                app_name: Application(charm=charms[app_name], config=d["configs"][app_name])
                for app_name in charms
            },
            integrations=d["relations"],
            platform=platform,
            arch=arch,
        )
    )
