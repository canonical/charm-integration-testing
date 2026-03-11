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


import yaml
from pydantic import BaseModel, ConfigDict, Field

from .charm import Charm, CharmConfig, EndpointType


class Application(BaseModel):
    charm: Charm
    config: CharmConfig = Field(default_factory=dict)

    def __repr__(self) -> str:
        return f"{self.charm.name}"


class ApplicationEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    application: str
    endpoint: str

    def __str__(self) -> str:
        return f"{self.application}:{self.endpoint}"

    def __repr__(self) -> str:
        return self.__str__()


Integration = frozenset[ApplicationEndpoint]


class Bundle(BaseModel):
    applications: dict[str, Application]
    integrations: set[Integration]
    platform: str
    arch: str

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
                        "options": {key: value for key, value in info.config.items() if value is not None},
                    }
                    for application, info in self.applications.items()
                },
                "bundle": self.platform,
                "relations": sorted(
                    [
                        sorted(
                            [
                                f"{application_endpoint.application}:{application_endpoint.endpoint}"
                                for application_endpoint in sorted(
                                    integration, key=lambda ep: (ep.application, ep.endpoint)
                                )
                            ]
                        )
                        for integration in sorted(
                            self.integrations, key=lambda i: tuple(sorted((ep.application, ep.endpoint) for ep in i))
                        )
                    ]
                ),
            },
            default_flow_style=False,
            sort_keys=True,
        )

    def export_mermaid(self, markdown: bool = False) -> str:
        """Export bundle to mermaid graph string."""
        lines = ["graph TB"]

        # Add application nodes with detailed information
        for application in sorted(self.applications):
            info = self.applications[application]
            charm_info = f"{info.charm.channel} rev:{info.charm.revision}"
            if application == info.charm.name:
                # Application name matches charm name
                lines.append(f'    {application}["{application}<br/>{charm_info}"]')
            else:
                # Application has custom name
                lines.append(f'    {application}["{application}<br/>({info.charm.name})<br/>{charm_info}"]')

        lines.append("")  # Blank line for readability

        # Add integrations with endpoint names as labels
        for integration in sorted(
            self.integrations,
            key=lambda i: (
                min((e.application, e.endpoint) for e in i),
                max((e.application, e.endpoint) for e in i),
            ),
        ):
            ep1, ep2 = sorted(integration, key=lambda e: (e.application, e.endpoint))
            charm_ep1 = self.applications[ep1.application].charm.endpoints[ep1.endpoint]
            interface = charm_ep1.interface

            # Determine which endpoint is requirers
            if charm_ep1.type == EndpointType.REQUIRES:
                requirer_ep = ep1
                provider_ep = ep2
            else:
                requirer_ep = ep2
                provider_ep = ep1

            # Escape angle brackets for Mermaid
            label = f"{provider_ep.endpoint}&lt;{interface}&gt;{requirer_ep.endpoint}"
            lines.append(f"    {provider_ep.application} -->|{label}| {requirer_ep.application}")

        result = "\n".join(lines) + "\n"

        if markdown:
            result = f"```mermaid\n{result}```\n"

        return result
