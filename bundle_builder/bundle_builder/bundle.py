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

from .charm import Charm, CharmEndpoint


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

        # Collect all non-optional application endpoints
        non_optional_endpoints = {
            ApplicationEndpoint(application=application.name, endpoint=endpoint.name)
            for application in self.applications
            for endpoint in application.charm.endpoints
            if not endpoint.optionality.is_optional(
                {endpoint.endpoint for endpoint in fulfilled_endpoints if endpoint.application == application.name}
            )
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

    # Export bundle to yaml string
    def export(self) -> str:
        return yaml.dump(
            {
                "applications": {
                    application.name: {
                        "charm": application.charm.name,
                        "channel": application.charm.channel,
                        "revision": application.charm.revision,
                        "base": f"ubuntu@{application.charm.ubuntu_version}",
                        "scale": 1,
                        "trust": True,
                    }
                    for application in self.applications
                },
                "bundle": self.platform,
                "relations": [
                    [
                        f"{application_endpoint.application}:{application_endpoint.endpoint}"
                        for application_endpoint in sorted(integration)
                    ]
                    for integration in sorted(self.integrations)
                ],
            },
            default_flow_style=False,
            sort_keys=True,
        )
