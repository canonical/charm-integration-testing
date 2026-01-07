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
from pydantic.dataclasses import dataclass

from .charm import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, Charm, CharmConfig, CharmEndpoint
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

    def validate(self):
        # Ensure all applications have unique names
        if len(self.application_lookup) != len(self.applications):
            raise ValueError("Application names must be unique in the bundle.")

        # Validate integrations
        for integration in self.integrations:
            # Ensure integrations connect exactly two endpoints
            if len(integration) != 2:
                raise ValueError("Each integration must connect exactly two endpoints.")

            # Ensure all integrations reference valid applications and endpoints
            for app_endpoint in integration:
                if app_endpoint not in self.application_endpoints:
                    raise ValueError(f"Integration references unknown endpoint '{app_endpoint}'")

            # Ensure integration does not connect endpoints with different interfaces
            ep1, ep2 = list(integration)
            charm_ep1 = self.application_endpoints[ep1]
            charm_ep2 = self.application_endpoints[ep2]
            if charm_ep1.interface != charm_ep2.interface:
                raise ValueError(
                    f"Integration connects endpoints with different interfaces: '{ep1}' ({charm_ep1.interface}) "
                    f"and '{ep2}' ({charm_ep2.interface})"
                )

            # Ensure integration connects different applications
            if len({ep.application for ep in integration}) < len(integration):
                raise ValueError(f"Integration must connect different applications: '{integration}'")

            # Ensure integration connects compatible endpoint types
            if charm_ep1.type == ENDPOINT_REQUIRES and charm_ep2.type == ENDPOINT_PROVIDES:
                requirer_ep, provider_ep = ep1, ep2
                requirer_charm_ep, provider_charm_ep = charm_ep1, charm_ep2
            elif charm_ep1.type == ENDPOINT_PROVIDES and charm_ep2.type == ENDPOINT_REQUIRES:
                requirer_ep, provider_ep = ep2, ep1
                requirer_charm_ep, provider_charm_ep = charm_ep2, charm_ep1
            else:
                raise ValueError(
                    f"Incompatible endpoint types in integration: '{ep1}' ({charm_ep1.type}) "
                    f"and '{ep2}' ({charm_ep2.type})"
                )

            # Ensure provider provides all features of the requirer
            if not provider_charm_ep.features >= requirer_charm_ep.features:
                raise ValueError(
                    f"Provider endpoint '{provider_ep}' does not provide all "
                    f"features required by '{requirer_ep}': {requirer_charm_ep.features - provider_charm_ep.features}"
                )

        # Ensure endpoints are not integrated more than their limit
        for endpoint, count in self.endpoint_connection_counts.items():
            charm_endpoint = self.application_endpoints[endpoint]
            endpoint_limit = charm_endpoint.limit(self.application_to_integrated_endpoints[endpoint.application])
            if endpoint_limit is not None and count > endpoint_limit:
                raise ValueError(
                    f"Endpoint '{endpoint}' is connected {count} times, exceeding its limit of {endpoint_limit}"
                )

    @computed_property
    def application_lookup(self) -> dict[str, Application]:
        return {application.name: application for application in self.applications}

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
        # PERF: It is faster to create tuple keys than ApplicationEndpoint in queries below
        counts = {(k.application, k.endpoint): v for k, v in self.endpoint_connection_counts.items()}

        # Check if they are saturated
        for app in self.applications:
            for endpoint in app.charm.endpoints:
                endpoint_limit = endpoint.limit(self.application_to_integrated_endpoints[app.name])
                if endpoint_limit is not None:
                    # Get the current connection count (default to 0 if not in counts)
                    current_count = counts.get((app.name, endpoint.name), 0)
                    if current_count >= endpoint_limit:
                        application_endpoint = ApplicationEndpoint(application=app.name, endpoint=endpoint.name)
                        saturated_endpoints.add(application_endpoint)

        return frozenset(saturated_endpoints)

    @computed_property
    def application_to_integrated_endpoints(self) -> dict[str, frozenset[str]]:
        map = {application.name: set() for application in self.applications}
        for integration in self.integrations:
            for endpoint in integration:
                map[endpoint.application].add(endpoint.endpoint)
        return {application: frozenset(endpoints) for application, endpoints in map.items()}

    @computed_property
    def application_endpoint_features(self) -> dict[str, frozenset[str]]:
        # Build provider-to-requirers mapping, but only for endpoints with features
        # PERF: It is faster to create tuple keys than ApplicationEndpoint in queries below
        provider_to_requirers: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for integration in self.integrations:
            ep1, ep2 = tuple(integration)
            charm_ep1 = self.application_endpoints[ep1]
            charm_ep2 = self.application_endpoints[ep2]

            # Skip if neither endpoint has features
            if not charm_ep1.features and not charm_ep2.features:
                continue

            if charm_ep1.type == ENDPOINT_REQUIRES:
                requirer_ep, provider_ep = (ep1.application, ep1.endpoint), (ep2.application, ep2.endpoint)
            else:
                requirer_ep, provider_ep = (ep2.application, ep2.endpoint), (ep1.application, ep1.endpoint)

            if provider_ep not in provider_to_requirers:
                provider_to_requirers[provider_ep] = set()
            provider_to_requirers[provider_ep].add(requirer_ep)

        # Only iterate through applications that have endpoints with features
        map: dict[str, set[str]] = {}
        for application in self.applications:
            app_features: set[str] = set()

            for endpoint in application.charm.endpoints:
                # Skip endpoints without features immediately
                if not endpoint.features:
                    continue

                # Check if this endpoint is integrated
                if endpoint.name not in self.application_to_integrated_endpoints[application.name]:
                    continue

                if endpoint.type == ENDPOINT_REQUIRES:
                    # For requiring endpoints: return all its features if integrated
                    for feature in endpoint.features:
                        app_features.add(f"{endpoint.name}:{feature}")
                elif endpoint.type == ENDPOINT_PROVIDES:
                    # For providing endpoints: return union of features required by all connected requirers
                    app_endpoint_tuple = (application.name, endpoint.name)
                    requirers = provider_to_requirers.get(app_endpoint_tuple, [])
                    if requirers:
                        connected_features = [
                            self.application_endpoints[ApplicationEndpoint(req[0], req[1])].features
                            for req in requirers
                        ]
                        used_features = frozenset().union(*connected_features)
                        for feature in used_features:
                            app_features.add(f"{endpoint.name}:{feature}")

            map[application.name] = app_features

        # Ensure all applications are in the result, even if they have no features
        return {app.name: frozenset(map.get(app.name, set())) for app in self.applications}

    @computed_property
    def unfulfilled_endpoints(self) -> frozenset[ApplicationEndpoint]:
        # Collect all fulfilled application endpoints
        fulfilled_endpoints = {endpoint for integration in self.integrations for endpoint in integration}

        # Collect all saturated endpoints
        saturated_endpoints = self.saturated_endpoints

        # Collect all non-optional endpoints
        non_optional_endpoints = set()
        for application in self.applications:
            for endpoint in application.charm.endpoints:
                if endpoint.optionality.is_optional(
                    self.application_to_integrated_endpoints[application.name],
                    self.application_endpoint_features[application.name],
                ):
                    continue
                non_optional_endpoints.add(ApplicationEndpoint(application=application.name, endpoint=endpoint.name))

        return frozenset(non_optional_endpoints - fulfilled_endpoints - saturated_endpoints)

    @immutable_dataclass
    class EndpointDependency:
        application: str
        dependent_endpoint: str

    @dataclass
    class EndpointDependencies:
        provides: set["Bundle.EndpointDependency"] = Field(default_factory=set)
        requires: set["Bundle.EndpointDependency"] = Field(default_factory=set)

    @computed_property
    def dependency_graph(self) -> dict[str, EndpointDependencies]:
        """Return a graph mapping each application to its endpoint dependencies."""
        graph = {app.name: Bundle.EndpointDependencies() for app in self.applications}
        for integration in self.integrations:
            ep1, ep2 = list(integration)
            charm_ep1 = self.application_endpoints[ep1]
            charm_ep2 = self.application_endpoints[ep2]
            if charm_ep1.type == ENDPOINT_REQUIRES and charm_ep2.type == ENDPOINT_PROVIDES:
                graph[ep1.application].requires.add(
                    Bundle.EndpointDependency(application=ep2.application, dependent_endpoint=charm_ep1.name)
                )
                graph[ep2.application].provides.add(
                    Bundle.EndpointDependency(application=ep1.application, dependent_endpoint=charm_ep2.name)
                )
            elif charm_ep1.type == ENDPOINT_PROVIDES and charm_ep2.type == ENDPOINT_REQUIRES:
                graph[ep2.application].requires.add(
                    Bundle.EndpointDependency(application=ep1.application, dependent_endpoint=charm_ep2.name)
                )
                graph[ep1.application].provides.add(
                    Bundle.EndpointDependency(application=ep2.application, dependent_endpoint=charm_ep1.name)
                )
        return graph

    def has_application_dependency(self, dependent_application: str, depended_on_application: str) -> bool:
        """Return True if dependent_application depends on depended_on_application."""
        visited = set()
        stack = [dependent_application]
        while stack:
            application = stack.pop()
            if application == depended_on_application:
                return True
            if application in visited:
                continue
            visited.add(application)
            for dependency in self.dependency_graph[application].requires:
                stack.append(dependency.application)
        return False

    def has_endpoint_dependency(
        self, application: str, charm_name: str, charm_endpoint: str, endpoint_type: str
    ) -> bool:
        """Return True if application depends on a specific charm endpoint."""
        visited = set()
        stack = [application]
        while stack:
            current_app = stack.pop()
            if current_app in visited:
                continue
            visited.add(current_app)
            if endpoint_type == ENDPOINT_PROVIDES:
                dependencies = self.dependency_graph[current_app].provides
            else:
                dependencies = self.dependency_graph[current_app].requires
            for dependency in dependencies:
                if (
                    self.application_lookup[current_app].charm.name == charm_name
                    and dependency.dependent_endpoint == charm_endpoint
                ):
                    return True
                stack.append(dependency.application)
        return False

    def generate_unique_application_name(self, charm_name: str) -> str:
        """Generate a unique application name for a charm, adding suffix if needed."""

        # Gather existing application names
        existing_names = self.application_lookup.keys()

        # If the base charm name is not taken, use it
        if charm_name not in existing_names:
            return charm_name

        # Otherwise, append alphabetic suffixes
        def _num_to_letters(n: int) -> str:
            # Convert 1 -> 'a', 2 -> 'b', ..., 26 -> 'z', 27 -> 'aa', etc.
            letters: list[str] = []
            while n > 0:
                n -= 1
                letters.append(chr(ord("a") + (n % 26)))
                n //= 26
            return "".join(reversed(letters))

        counter = 1
        candidate = f"{charm_name}-{_num_to_letters(counter)}"
        while candidate in existing_names:
            counter += 1
            candidate = f"{charm_name}-{_num_to_letters(counter)}"

        return candidate

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
                    application.name: {
                        "charm": application.charm.name,
                        "channel": str(application.charm.channel),
                        "revision": application.charm.revision,
                        "base": f"ubuntu@{application.charm.ubuntu_version}",
                        scale_key: 1,
                        "trust": True,
                        "options": {key: value for key, value in application.config},
                    }
                    for application in self.applications
                },
                "bundle": self.platform,
                "relations": sorted(
                    [
                        sorted(
                            [
                                f"{application_endpoint.application}:{application_endpoint.endpoint}"
                                for application_endpoint in sorted(integration)
                            ]
                        )
                        for integration in sorted(self.integrations)
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
            ep1, ep2 = sorted(integration)
            charm_ep1 = self.application_endpoints[ep1]
            interface = charm_ep1.interface

            # Determine which endpoint is requirers
            if charm_ep1.type == ENDPOINT_REQUIRES:
                requirer_ep = ep1
                provider_ep = ep2
            else:
                requirer_ep = ep2
                provider_ep = ep1

            # Escape angle brackets for Mermaid
            label = f"{provider_ep.endpoint}&lt;{interface}&gt;{requirer_ep.endpoint}"
            lines.append(f"    {provider_ep.application} -->|{label}| {requirer_ep.application}")

        return "\n".join(lines) + "\n"
