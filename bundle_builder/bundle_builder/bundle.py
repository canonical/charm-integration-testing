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


from dataclasses import dataclass
from functools import cached_property, total_ordering
import yaml

from .charm import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, Charm, CharmEndpoint


@dataclass(frozen=True)
class Application:
    name: str
    charm: Charm

    def __repr__(self):
        if self.name == self.charm.name:
            return f"{self.name}"
        else:
            return f"{self.name}({self.charm.name})"


@total_ordering
@dataclass(frozen=True)
class ApplicationEndpoint:
    application: str
    endpoint: str

    def __str__(self):
        return f"{self.application}:{self.endpoint}"

    def __repr__(self):
        return self.__str__()
    
    def __lt__(self, other):
        if not isinstance(other, ApplicationEndpoint):
            return NotImplemented
        return str(self) < str(other)


Integration = frozenset[ApplicationEndpoint]


@dataclass(frozen=True)
class Bundle:
    applications: frozenset[Application]
    integrations: frozenset[Integration]
    platform: str
    arch: str

    @cached_property
    def application_endpoints(self) -> dict[ApplicationEndpoint, CharmEndpoint]:
        return {
            ApplicationEndpoint(application=application.name, endpoint=endpoint.name): endpoint
            for application in self.applications
            for endpoint in application.charm.endpoints
        }

    @cached_property
    def charms(self) -> frozenset[str]:
        return frozenset({application.charm.name for application in self.applications})

    @cached_property
    def unfulfilled_endpoints(self) -> frozenset[ApplicationEndpoint]:
        # Collect all fulfilled application endpoints
        fulfilled_endpoints = {endpoint for integration in self.integrations for endpoint in integration}

        # Collect all non-optional requires application endpoints
        non_optional_endpoints = {
            ApplicationEndpoint(application=application.name, endpoint=endpoint.name)
            for application in self.applications
            for endpoint in application.charm.endpoints
            if not endpoint.optional and endpoint.type == ENDPOINT_REQUIRES
        }

        # Return the difference
        return frozenset(non_optional_endpoints - fulfilled_endpoints)

    @cached_property
    def unfulfilled_interfaces(self) -> frozenset[str]:
        return frozenset(
            {
                self.application_endpoints[application_endpoint].interface
                for application_endpoint in self.unfulfilled_endpoints
            }
        )

    def add_missing_integrations(self) -> "Bundle":
        # Check if any applications provide an endpoint that fulfills all unfulfilled integrations
        new_integrations = set()
        for unfulfilled_application_endpoint in self.unfulfilled_endpoints:
            unfulfilled_charm_endpoint = self.application_endpoints[unfulfilled_application_endpoint]

            # Check all potential application endpoints
            for possible_application_endpoint, possible_charm_endpoint in self.application_endpoints.items():
                # Will not integrate with self
                if possible_application_endpoint.application == unfulfilled_application_endpoint.application:
                    continue
                # Will not integrate different interfaces
                if possible_charm_endpoint.interface != unfulfilled_charm_endpoint.interface:
                    continue
                # Will not integrate wrong endpoint types
                if not (
                    (
                        possible_charm_endpoint.type == ENDPOINT_REQUIRES
                        and unfulfilled_charm_endpoint.type == ENDPOINT_PROVIDES
                    )
                    or (
                        possible_charm_endpoint.type == ENDPOINT_PROVIDES
                        and unfulfilled_charm_endpoint.type == ENDPOINT_REQUIRES
                    )
                ):
                    continue

                # Integration is good, add it
                new_integrations.add(Integration({unfulfilled_application_endpoint, possible_application_endpoint}))
                break

        # Return the bundle with the new integrations
        return self.__class__(
            applications=self.applications,
            integrations=frozenset(self.integrations | new_integrations),
            platform=self.platform,
            arch=self.arch,
        )

    # Export bundle to yaml string
    def export(self) -> str:
        return yaml.dump({
            "applications": {
                application.name: {
                    "charm": application.charm.name,
                    "channel": application.charm.channel,
                    "revision": application.charm.revision,
                    "scale": 1,
                }
                for application in self.applications
            },
            "bundle": self.platform,
            "relations": [
                [f"{application_endpoint.application}:{application_endpoint.endpoint}" for application_endpoint in sorted(integration)]
                for integration in sorted(self.integrations)
            ]
        }, default_flow_style=False, sort_keys=True)
