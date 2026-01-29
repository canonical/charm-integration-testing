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
from pydantic import BaseModel, Field

from .charm import Charm, CharmConfig


class Application(BaseModel):
    charm: Charm
    config: CharmConfig = Field(default_factory=CharmConfig)

    def __repr__(self) -> str:
        if self.name == self.charm.name:
            return f"{self.name}"
        else:
            return f"{self.name}({self.charm.name})"


class ApplicationEndpoint(BaseModel):
    application: str
    endpoint: str

    def __str__(self) -> str:
        return f"{self.application}:{self.endpoint}"

    def __repr__(self) -> str:
        return self.__str__()


class Integration(BaseModel):
    requirer: ApplicationEndpoint
    provider: ApplicationEndpoint

    def __iter__(self):
        yield self.requirer
        yield self.provider

    def __repr__(self) -> str:
        return f"{self.requirer} <-> {self.provider}"


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
