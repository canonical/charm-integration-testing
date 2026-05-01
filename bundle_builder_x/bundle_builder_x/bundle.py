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


from collections import defaultdict
from collections.abc import Iterator

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .charm import Charm, CharmConfigValue, EndpointType
from .juju_version import JujuVersion

# Mermaid reserved keywords that cannot be used bare as subgraph/node IDs.
_MERMAID_RESERVED = frozenset(
    {"default", "end", "subgraph", "graph", "class", "click", "style", "linkstyle", "classDef"}
)


_IND = "    "  # one Mermaid indent level (4 spaces)


def _mermaid_id(name: str) -> str:
    """Return a Mermaid-safe identifier for a model name."""
    if name.lower() in _MERMAID_RESERVED:
        return f"m_{name}"
    return name


def _mermaid_node_id(model_id: str, application: str) -> str:
    """Return a namespaced Mermaid node ID to avoid collisions across models."""
    return f"{model_id}__{application}"


class Application(BaseModel):
    charm: Charm
    config: dict[str, CharmConfigValue] = Field(default_factory=dict)

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


class Integration(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoints: tuple[ApplicationEndpoint, ApplicationEndpoint]

    @classmethod
    def create(cls, ep_1: ApplicationEndpoint, ep_2: ApplicationEndpoint) -> "Integration":
        """Create an Integration, sorting endpoints canonically so equality is order-independent."""
        eps = sorted([ep_1, ep_2], key=lambda ep: (ep.application, ep.endpoint))
        return cls(endpoints=(eps[0], eps[1]))

    def __iter__(self) -> Iterator["ApplicationEndpoint"]:  # type: ignore[override]  # intentionally iterates endpoints, not field tuples
        return iter(self.endpoints)


class CrossModelIntegration(BaseModel):
    """A cross-model integration between a local endpoint and a remote model's endpoint."""

    model_config = ConfigDict(frozen=True)

    local: ApplicationEndpoint
    local_role: EndpointType
    remote_model: str
    remote_application: str
    remote_endpoint: str
    offer_name: str
    url: str | None = None


class Bundle(BaseModel):
    model: str | None = None
    controller: str | None = None
    applications: dict[str, Application]
    integrations: set[Integration]
    cross_model_integrations: list[CrossModelIntegration] = Field(default_factory=list)
    platform: str
    arch: str
    juju_version: JujuVersion

    def export(self) -> str:
        # Validate platform is supported
        if self.platform not in ["kubernetes", "machine"]:
            raise ValueError(f"Unsupported platform: {self.platform}")

        # Determine the correct scale/unit key based on platform
        scale_key = "scale" if self.platform == "kubernetes" else "num_units"

        # Build offers grouped by (local_application, offer_name) -> list of endpoints
        offers_by_app: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for cmr in self.cross_model_integrations:
            if cmr.local_role == EndpointType.PROVIDES:
                offer_endpoints = offers_by_app[cmr.local.application][cmr.offer_name]
                if cmr.local.endpoint not in offer_endpoints:
                    offer_endpoints.append(cmr.local.endpoint)

        # Build saas entries for consuming side
        saas_entries: dict[str, dict[str, str]] = {}
        for cmr in self.cross_model_integrations:
            if cmr.local_role == EndpointType.REQUIRES and cmr.url is not None:
                saas_entries[cmr.offer_name] = {"url": cmr.url}

        # Build applications dict (no offers in the base bundle)
        applications_dict: dict[str, dict[str, object]] = {}
        for application, info in self.applications.items():
            app_dict: dict[str, object] = {
                "charm": info.charm.name,
                "channel": str(info.charm.channel),
                "revision": info.charm.revision,
                "base": f"ubuntu@{info.charm.ubuntu_version}",
                scale_key: 1,
                "trust": True,
                "options": {key: value for key, value in info.config.items() if value is not None},
            }
            applications_dict[application] = app_dict

        # Build relations list including cross-model relations
        local_relations = sorted(
            [
                sorted(
                    [
                        f"{application_endpoint.application}:{application_endpoint.endpoint}"
                        for application_endpoint in sorted(integration, key=lambda ep: (ep.application, ep.endpoint))
                    ]
                )
                for integration in sorted(
                    self.integrations, key=lambda i: tuple(sorted((ep.application, ep.endpoint) for ep in i))
                )
            ]
        )

        cmr_relations = sorted(
            [
                sorted(
                    [
                        f"{cmr.local.application}:{cmr.local.endpoint}",
                        f"{cmr.offer_name}:{cmr.remote_endpoint}",
                    ]
                )
                for cmr in self.cross_model_integrations
                if cmr.local_role == EndpointType.REQUIRES
            ]
        )

        bundle_dict: dict[str, object] = {
            "applications": applications_dict,
            "bundle": self.platform,
            "relations": local_relations + cmr_relations,
        }

        # Add saas section if there are consuming CMRs
        if saas_entries:
            bundle_dict["saas"] = dict(sorted(saas_entries.items()))

        base_yaml = yaml.dump(bundle_dict, default_flow_style=False, sort_keys=True)

        # Offers cannot appear in the base bundle section; emit them as a bundle overlay
        # (second YAML document) so that Juju accepts them.
        if offers_by_app:
            overlay_apps: dict[str, object] = {
                application: {
                    "offers": {
                        offer_name: {"endpoints": sorted(endpoints)}
                        for offer_name, endpoints in sorted(app_offers.items())
                    }
                }
                for application, app_offers in sorted(offers_by_app.items())
            }
            overlay_dict: dict[str, object] = {"applications": overlay_apps}
            overlay_yaml = yaml.dump(overlay_dict, default_flow_style=False, sort_keys=True)
            return f"---\n{base_yaml}---\n{overlay_yaml}"

        return base_yaml


def _mermaid_subgraph_lines(bundle: Bundle, model_name: str, model_id: str) -> list[str]:
    """Return Mermaid lines for one model subgraph: application nodes and local integration edges."""
    lines: list[str] = [
        f"{_IND}subgraph {model_id}[{model_name}]",
        f"{_IND * 2}direction TB",
    ]

    for application in sorted(bundle.applications):
        info = bundle.applications[application]
        node_id = _mermaid_node_id(model_id, application)
        charm_info = f"{info.charm.channel} rev:{info.charm.revision}"
        if application == info.charm.name:
            lines.append(f'{_IND * 2}{node_id}["{application}<br/>{charm_info}"]:::app')
        else:
            lines.append(f'{_IND * 2}{node_id}["{application}<br/>({info.charm.name})<br/>{charm_info}"]:::app')

    lines.append("")

    for integration in sorted(
        bundle.integrations,
        key=lambda i: (
            min((e.application, e.endpoint) for e in i),
            max((e.application, e.endpoint) for e in i),
        ),
    ):
        ep_1, ep_2 = sorted(integration, key=lambda e: (e.application, e.endpoint))
        charm_ep_1 = bundle.applications[ep_1.application].charm.endpoints[ep_1.endpoint]
        interface = charm_ep_1.interface
        if charm_ep_1.type == EndpointType.REQUIRES:
            requirer_ep, provider_ep = ep_1, ep_2
        else:
            requirer_ep, provider_ep = ep_2, ep_1
        label = f"{provider_ep.endpoint}<br/>&lt;{interface}&gt;<br/>{requirer_ep.endpoint}"
        provider_id = _mermaid_node_id(model_id, provider_ep.application)
        requirer_id = _mermaid_node_id(model_id, requirer_ep.application)
        lines.append(f'{_IND * 2}{provider_id} -->|"{label}"| {requirer_id}')

    lines.append(f"{_IND}end")
    return lines


def _mermaid_cmr_edge_lines(bundle: Bundle, model_id: str) -> list[str]:
    """Return Mermaid edge lines for PROVIDES-side cross-model integrations."""
    lines: list[str] = []
    for cmr in sorted(
        bundle.cross_model_integrations,
        key=lambda c: (c.local.application, c.local.endpoint, c.remote_model, c.remote_application),
    ):
        if cmr.local_role != EndpointType.PROVIDES:
            continue
        local_id = _mermaid_node_id(model_id, cmr.local.application)
        remote_id = _mermaid_node_id(_mermaid_id(cmr.remote_model), cmr.remote_application)
        interface = bundle.applications[cmr.local.application].charm.endpoints[cmr.local.endpoint].interface
        label = f"{cmr.local.endpoint}<br/>&lt;{interface}&gt;<br/>{cmr.remote_endpoint}"
        lines.append(f'{_IND}{local_id} -.->|"{label}"| {remote_id}')
    return lines


class Solution(BaseModel):
    """The full multi-model result produced by BundleBuilder."""

    bundles: list[Bundle]

    def export_mermaid(self, markdown: bool = False) -> str:
        """Export all models as one Mermaid graph with subgraphs per model.

        Cross-model integration edges are drawn across subgraph boundaries.
        """
        sorted_bundles = sorted(self.bundles, key=lambda b: b.model or "")

        lines: list[str] = [
            "%%{init: {'theme': 'base', 'themeVariables': {'lineColor': '#64748b', 'edgeLabelBackground': '#f8fafc'}}}%%",
            "graph TB",
            f"{_IND}classDef app fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f",
            "",
        ]

        # Render each model as a subgraph, collecting one anchor node per model for ordering
        anchor_nodes: list[str] = []
        for bundle in sorted_bundles:
            model_name = bundle.model or "_default"
            model_id = _mermaid_id(model_name)
            lines.extend(_mermaid_subgraph_lines(bundle, model_name, model_id))
            lines.append(f"{_IND}style {model_id} fill:#f0f9ff,stroke:#0ea5e9,color:#0c4a6e")
            lines.append("")
            if bundle.applications:
                anchor_nodes.append(_mermaid_node_id(model_id, sorted(bundle.applications)[0]))

        # Invisible links between consecutive anchor nodes force subgraphs into a vertical stack
        for top, bottom in zip(anchor_nodes, anchor_nodes[1:]):
            lines.append(f"{_IND}{top} ~~~ {bottom}")
        if len(anchor_nodes) > 1:
            lines.append("")

        # Cross-model edges - rendered once from the PROVIDES side to avoid duplicates
        cmr_lines: list[str] = []
        for bundle in sorted_bundles:
            model_id = _mermaid_id(bundle.model or "_default")
            cmr_lines.extend(_mermaid_cmr_edge_lines(bundle, model_id))
        lines.extend(cmr_lines)
        if cmr_lines:
            lines.append("")

        result = "\n".join(lines) + "\n"
        if markdown:
            result = f"```mermaid\n{result}```\n"
        return result
