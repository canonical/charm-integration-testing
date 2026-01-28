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
from pydantic import BaseModel, ConfigDict

from .charm import Charm


class Application(BaseModel):
    model_config = ConfigDict(frozen=True)

    charm: Charm

    def __repr__(self) -> str:
        if self.name == self.charm.name:
            return f"{self.name}"
        else:
            return f"{self.name}({self.charm.name})"


class ApplicationEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    application: str
    endpoint: str

    def __str__(self) -> str:
        return f"{self.application}:{self.endpoint}"

    def __repr__(self) -> str:
        return self.__str__()


class Integration(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirer: ApplicationEndpoint
    provider: ApplicationEndpoint

    def __iter__(self):
        yield self.requirer
        yield self.provider

    def __repr__(self) -> str:
        return f"{self.requirer} <-> {self.provider}"


class Bundle(BaseModel):
    model_config = ConfigDict(frozen=True)

    applications: dict[str, Application]
    integrations: set[Integration]
    platform: str
    arch: str

    # def validate(self) -> None:
    #     # Ensure all applications have unique names
    #     if len(self.application_lookup) != len(self.applications):
    #         raise ValueError("Application names must be unique in the bundle.")

    #     # Validate integrations
    #     for integration in self.integrations:
    #         # Ensure integrations connect exactly two endpoints
    #         if len(integration) != 2:
    #             raise ValueError("Each integration must connect exactly two endpoints.")

    #         # Ensure all integrations reference valid applications and endpoints
    #         for app_endpoint in integration:
    #             if app_endpoint not in self.application_endpoints:
    #                 raise ValueError(f"Integration references unknown endpoint '{app_endpoint}'")

    #         # Ensure integration does not connect endpoints with different interfaces
    #         ep1, ep2 = list(integration)
    #         charm_ep1 = self.application_endpoints[ep1]
    #         charm_ep2 = self.application_endpoints[ep2]
    #         if charm_ep1.interface != charm_ep2.interface:
    #             raise ValueError(
    #                 f"Integration connects endpoints with different interfaces: '{ep1}' ({charm_ep1.interface}) "
    #                 f"and '{ep2}' ({charm_ep2.interface})"
    #             )

    #         # Ensure integration connects different applications
    #         if len({ep.application for ep in integration}) < len(integration):
    #             raise ValueError(f"Integration must connect different applications: '{integration}'")

    #         # Ensure integration connects compatible endpoint types
    #         if charm_ep1.type == ENDPOINT_REQUIRES and charm_ep2.type == ENDPOINT_PROVIDES:
    #             requirer_ep, provider_ep = ep1, ep2
    #             requirer_charm_ep, provider_charm_ep = charm_ep1, charm_ep2
    #         elif charm_ep1.type == ENDPOINT_PROVIDES and charm_ep2.type == ENDPOINT_REQUIRES:
    #             requirer_ep, provider_ep = ep2, ep1
    #             requirer_charm_ep, provider_charm_ep = charm_ep2, charm_ep1
    #         else:
    #             raise ValueError(
    #                 f"Incompatible endpoint types in integration: '{ep1}' ({charm_ep1.type}) "
    #                 f"and '{ep2}' ({charm_ep2.type})"
    #             )

    #         # Ensure provider provides all features of the requirer
    #         if not provider_charm_ep.features >= requirer_charm_ep.features:
    #             raise ValueError(
    #                 f"Provider endpoint '{provider_ep}' does not provide all "
    #                 f"features required by '{requirer_ep}': {requirer_charm_ep.features - provider_charm_ep.features}"
    #             )

    #     # Ensure endpoints are not integrated more than their limit
    #     for endpoint, count in self.endpoint_connection_counts.items():
    #         charm_endpoint = self.application_endpoints[endpoint]
    #         endpoint_limit = charm_endpoint.limit(self.application_to_integrated_endpoints[endpoint.application])
    #         if endpoint_limit is not None and count > endpoint_limit:
    #             raise ValueError(
    #                 f"Endpoint '{endpoint}' is connected {count} times, exceeding its limit of {endpoint_limit}"
    #             )

    def export(self) -> str:
        """Export bundle to yaml string."""

        # Validate platform is supported
        if self.platform not in ["kubernetes", "machine"]:
            raise ValueError(f"Unsupported platform: {self.platform}")

        # Determine the correct scale/unit key based on platform
        scale_key = "scale" if self.platform == "kubernetes" else "num_units"

        return yaml.dump(
            {
                "applications": {
                    application: {
                        "charm": info.charm.name,
                        "channel": str(info.charm.channel),
                        "revision": info.charm.revision,
                        "base": f"ubuntu@{info.charm.ubuntu_version}",
                        scale_key: 1,
                        "trust": True,
                        # "options": {key: value for key, value in application.config},
                    }
                    for application, info in self.applications.items()
                },
                "bundle": self.platform,
                "relations": sorted(
                    [
                        [
                            f"{integration.provider.application}:{integration.provider.endpoint}",
                            f"{integration.requirer.application}:{integration.requirer.endpoint}",
                        ]
                        for integration in self.integrations
                    ]
                ),
            },
            default_flow_style=False,
            sort_keys=True,
        )

    def export_mermaid(self) -> str:
        """Export bundle to mermaid graph string."""
        lines = ["graph TB"]

        # Add application nodes with detailed information
        for application in sorted(self.applications, key=lambda a: a.name):
            charm_info = f"{application.charm.channel} rev:{application.charm.revision}"
            if application.name == application.charm.name:
                # Application name matches charm name
                lines.append(f'    {application.name}["{application.name}<br/>{charm_info}"]')
            else:
                # Application has custom name
                lines.append(
                    f'    {application.name}["{application.name}<br/>({application.charm.name})<br/>{charm_info}"]'
                )

        lines.append("")  # Blank line for readability

        # Add integrations with endpoint names as labels
        for integration in sorted(self.integrations):
            interface = (
                self.applications[integration.requirer.application]
                .charm.endpoints[integration.requirer.endpoint]
                .interface
            )

            # Escape angle brackets for Mermaid
            label = f"{integration.provider.endpoint}&lt;{interface}&gt;{integration.requirer.endpoint}"
            lines.append(f"    {integration.provider.application} -->|{label}| {integration.requirer.application}")

        return "\n".join(lines) + "\n"
