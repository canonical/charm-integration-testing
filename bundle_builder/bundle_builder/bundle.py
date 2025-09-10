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


import yaml
from pydantic import Field

from .charm import Charm, CharmConfig, CharmEndpoint
from .immutable_dataclass import computed_property, immutable_dataclass


@immutable_dataclass
class Application:
    name: str
    charm: Charm
    config: CharmConfig = Field(default_factory=CharmConfig)

    def __repr__(self):
        if self.name == self.charm.name:
            return f"{self.name}"
        else:
            return f"{self.name}({self.charm.name})"


@immutable_dataclass(order=True)
class ApplicationEndpoint:
    application: str
    endpoint: str

    def __str__(self):
        return f"{self.application}:{self.endpoint}"

    def __repr__(self):
        return self.__str__()


Integration = frozenset[ApplicationEndpoint]


@immutable_dataclass
class Bundle:
    applications: frozenset[Application]
    integrations: frozenset[Integration]
    platform: str
    arch: str

    @computed_property
    def application_endpoints(self) -> dict[ApplicationEndpoint, CharmEndpoint]:
        return {
            ApplicationEndpoint(application=application.name, endpoint=endpoint.name): endpoint
            for application in self.applications
            for endpoint in application.charm.endpoints
        }

    @computed_property
    def charms(self) -> frozenset[str]:
        return frozenset({application.charm.name for application in self.applications})

    @computed_property
    def endpoint_connection_counts(self) -> dict[ApplicationEndpoint, int]:
        counts = {}
        for integration in self.integrations:
            for endpoint in integration:
                if endpoint in counts:
                    counts[endpoint] += 1
                else:
                    counts[endpoint] = 1
        return counts

    @computed_property
    def saturated_endpoints(self) -> frozenset[ApplicationEndpoint]:
        saturated_endpoints = set()
        counts: dict[ApplicationEndpoint, int] = self.endpoint_connection_counts

        # Check if they are saturated
        for app in self.applications:
            for endpoint in app.charm.endpoints:
                application_endpoint = ApplicationEndpoint(application=app.name, endpoint=endpoint.name)
                if endpoint.limit is not None:
                    # Get the current connection count (default to 0 if not in counts)
                    current_count = counts.get(application_endpoint, 0)
                    if current_count >= endpoint.limit:
                        saturated_endpoints.add(application_endpoint)

        return frozenset(saturated_endpoints)

    @computed_property
    def unfulfilled_endpoints(self) -> frozenset[ApplicationEndpoint]:
        # Collect all fulfilled application endpoints
        fulfilled_endpoints = {endpoint for integration in self.integrations for endpoint in integration}

        non_optional_endpoints = {
            ApplicationEndpoint(application=application.name, endpoint=endpoint.name)
            for application in self.applications
            for endpoint in application.charm.endpoints
            if not endpoint.optionality.is_optional(
                {endpoint.endpoint for endpoint in fulfilled_endpoints if endpoint.application == application.name}
            )
        }
        return frozenset(non_optional_endpoints - fulfilled_endpoints - self.saturated_endpoints)

    @computed_property
    def unfulfilled_interfaces(self) -> frozenset[str]:
        return frozenset(
            {
                self.application_endpoints[application_endpoint].interface
                for application_endpoint in self.unfulfilled_endpoints
            }
        )

    def get_application_names_for_charm(self, charm_name: str) -> frozenset[str]:
        """Get all application names that use a specific charm."""
        return frozenset({app.name for app in self.applications if app.charm.name == charm_name})

    def generate_unique_application_name(self, charm_name: str) -> str:
        """Generate a unique application name for a charm, adding suffix if needed."""
        existing_names = {app.name for app in self.applications}

        # If the base charm name is not taken, use it
        if charm_name not in existing_names:
            return charm_name

        # Otherwise, find the next available suffix
        counter = 2
        while f"{charm_name}-{counter}" in existing_names:
            counter += 1

        return f"{charm_name}-{counter}"

    # Export bundle to yaml string
    def export(self) -> str:
        # Validate platform is supported
        if self.platform not in ["kubernetes", "machine"]:
            raise ValueError(f"Unsupported platform: {self.platform}")

        # Determine the correct scale/unit key based on platform
        scale_key = "scale" if self.platform == "kubernetes" else "num_units"

        return yaml.dump(
            {
                "applications": {
                    application.name: {
                        "charm": application.charm.name,
                        "channel": application.charm.channel,
                        "revision": application.charm.revision,
                        "base": f"ubuntu@{application.charm.ubuntu_version}",
                        scale_key: 1,
                        "trust": True,
                        "options": {key: value for key, value in application.config},
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
