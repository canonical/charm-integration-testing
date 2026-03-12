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


"""Bundle builder: orchestrates the iterative solve loop."""

import logging

from pydantic import BaseModel, ConfigDict
from hypothesis import find
from hypothesis.errors import NoSuchExample
from hypothesis import settings

from .bundle import ApplicationEndpoint, Bundle, Integration
from .charm import Charm, CharmChannel
from .charmhub import CharmhubClient
from .domain import initialize_domain
from .expand import expand
from .scriptlets import check_bundle, load_probes

_MAX_ITERATIONS = 10


class UnresolvableBundleError(Exception):
    def __init__(self, message: str):
        super().__init__(message)


class ApplicationConstraint(BaseModel):
    charm: str
    channel: CharmChannel | None = None
    revision: int | None = None
    base: str | None = None


class IntegrationConstraint(BaseModel):
    model_config = ConfigDict(frozen=True)

    application_1: str
    endpoint_1: str
    application_2: str
    endpoint_2: str


class BundleBuilder:
    def __init__(
        self,
        charmhub_client: CharmhubClient,
        logger: logging.Logger = logging.getLogger(__name__),
    ):
        self.charmhub_client = charmhub_client
        self.logger = logger

    def build(
        self,
        applications: dict[str, ApplicationConstraint],
        integrations: set[IntegrationConstraint],
        platform: str,
        arch: str,
        probe_urls: list[str] = [],
    ) -> Bundle:
        """Iteratively build a bundle that satisfies all probe constraints.

        Starts with the user-specified charm pool, runs Hypothesis to find a
        valid bundle, and expands the pool with new charms whenever a
        missing-relation signal is emitted. Raises UnresolvableBundleError if
        no valid bundle can be found within the iteration limit.
        """
        required_integrations: list[Integration] = [
            Integration.create(
                ApplicationEndpoint(application=ic.application_1, endpoint=ic.endpoint_1),
                ApplicationEndpoint(application=ic.application_2, endpoint=ic.endpoint_2),
            )
            for ic in integrations
        ]

        # Initial charm pool: app_name → Charm, fetched from user constraints.
        charms: dict[str, Charm] = {
            app_name: self.charmhub_client.charm_from_store(
                charm_name=constraint.charm,
                ubuntu_arch=arch,
                charm_channel=constraint.channel,
                charm_revision=constraint.revision,
                ubuntu_version=constraint.base,
            )
            for app_name, constraint in applications.items()
        }

        signals: list = []

        for iteration in range(_MAX_ITERATIONS):
            self.logger.info(
                f"Iteration {iteration + 1}/{_MAX_ITERATIONS} "
                f"with {len(charms)} application(s): {sorted(charms)}"
            )
            probes = load_probes(charms, extra_probe_urls=probe_urls)
            strategy = initialize_domain(charms, required_integrations, platform, arch)

            try:
                return find(strategy, lambda bundle: check_bundle(bundle, probes, signals), settings=settings(backend="hypofuzz", deadline=None))
            except NoSuchExample:
                new_charms = expand(signals, charms, self.charmhub_client, platform, arch, self.logger)
                if not new_charms:
                    raise UnresolvableBundleError(
                        "No bundle satisfying all probes could be found after expanding the charm pool"
                    )
                for charm_name in new_charms:
                    app_name = charm_name
                    suffix = 2
                    while app_name in charms:
                        app_name = f"{charm_name}-{suffix}"
                        suffix += 1
                    charms[app_name] = self.charmhub_client.charm_from_store(
                        charm_name=charm_name, ubuntu_arch=arch
                    )

        raise UnresolvableBundleError(
            "No bundle satisfying all probes could be found after expanding the charm pool"
        )