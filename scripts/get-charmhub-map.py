#!/usr/bin/env python
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import itertools
from typing import Dict, Set

import requests
import yaml
from pydantic.dataclasses import dataclass

CHARMHUB_API = "https://api.charmhub.io"


def query_http(url: str, params: dict | None = None) -> list | dict:
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def query_charmhub(path: str, params: dict | None = None) -> list | dict:
    return query_http(f"https://api.charmhub.io/{path}", params=params)


@dataclass(frozen=True)
class CharmWithRisk:
    name: str
    risk: str


def get_all_charms() -> set[CharmWithRisk]:
    charm_with_risk = set()
    for risk in ["stable", "candidate", "beta", "edge"]:
        results = query_charmhub("v2/charms/find", params={"type": "charm", "channel": risk})["results"]
        charm_with_risk |= {
            CharmWithRisk(
                name=result["name"],
                risk=risk,
            )
            for result in results
        }
    return charm_with_risk


@dataclass(frozen=True)
class CharmEndpoint:
    endpoint: str
    interface: str
    optional: bool

    @classmethod
    def from_metadata(cls, metadata: dict) -> set:
        return {
            cls(
                endpoint=endpoint,
                interface=integration["interface"],
                optional=integration.get("optional", False),
            )
            for endpoint, integration in metadata.items()
        }


@dataclass(frozen=True)
class CharmInfo:
    name: str
    has_deployable_on_kubernetes: bool
    has_k8s_api: bool
    requires: frozenset[CharmEndpoint]
    provides: frozenset[CharmEndpoint]


def get_info(charm: str) -> CharmInfo:
    result = query_charmhub(
        f"v2/charms/info/{charm}",
        params={
            "fields": ",".join(
                {
                    "default-release.revision.metadata-yaml",
                    "result.deployable-on",
                }
            )
        },
    )

    metadata = yaml.safe_load(result["default-release"]["revision"]["metadata-yaml"])

    return CharmInfo(
        name=result["name"],
        has_deployable_on_kubernetes="kubernetes" in result["result"]["deployable-on"],
        has_k8s_api="k8s-api" in metadata.get("assumes", {}),
        requires=frozenset(CharmEndpoint.from_metadata(metadata.get("requires", {}))),
        provides=frozenset(CharmEndpoint.from_metadata(metadata.get("provides", {}))),
    )


@dataclass(frozen=True)
class CharmIntegration:
    requirer: str | None
    requirer_endpoint: str | None
    provider: str | None
    provider_endpoint: str | None
    interface: str


def find_bundle_pairings(charm_infos: set[CharmInfo]) -> Dict[str, Set[CharmIntegration]]:
    charm_providers: Dict[str, Dict[str, str]] = {}  # interface -> {provider name -> provider endpoint}
    charm_requirers: Dict[str, Dict[str, str]] = {}  # interface -> {requirer name -> requirer endpoint}

    # Build mappings of interfaces to charms that provide and require them
    for charm in charm_infos:
        for provided in charm.provides:
            if provided.interface not in charm_providers:
                charm_providers[provided.interface] = {}
            charm_providers[provided.interface][charm.name] = provided.endpoint
        for required in charm.requires:
            if required.interface not in charm_requirers:
                charm_requirers[required.interface] = {}
            charm_requirers[required.interface][charm.name] = required.endpoint

    # Gather all the required endpoints for each charm and the possible integrations to complete them
    all_required_integrations: Dict[str, Dict[str, set[CharmIntegration]]] = {}  # charm -> {endpoint -> {integration}}
    for charm in charm_infos:
        integrations: Dict[str, Set[CharmIntegration]] = {}  # interface -> {integration}

        for endpoint in charm.requires:
            if endpoint.optional:
                continue  # Ignore optional requirements

            # Find a provider for the required interface
            if endpoint.interface in charm_providers:
                integrations[endpoint.endpoint] = {
                    CharmIntegration(
                        requirer=charm.name,
                        requirer_endpoint=endpoint.endpoint,
                        provider=provider_name,
                        provider_endpoint=provider_endpoint,
                        interface=endpoint.interface,
                    )
                    for provider_name, provider_endpoint in charm_providers[endpoint.interface].items()
                    if provider_name != charm.name
                }
            else:
                integrations[endpoint.endpoint] = {
                    CharmIntegration(
                        requirer=charm.name,
                        requirer_endpoint=endpoint.endpoint,
                        provider=None,
                        provider_endpoint=None,
                        interface=endpoint.interface,
                    )
                }

        for endpoint in charm.provides:
            if endpoint.optional:
                continue  # Ignore optional integrations

            # Find a provider for the required interface
            if endpoint.interface in charm_requirers:
                integrations[endpoint.endpoint] = {
                    CharmIntegration(
                        requirer=requirer_name,
                        requirer_endpoint=requirer_endpoint,
                        provider=charm.name,
                        provider_endpoint=endpoint.endpoint,
                        interface=endpoint.interface,
                    )
                    for requirer_name, requirer_endpoint in charm_requirers[endpoint.interface].items()
                    if requirer_name != charm.name
                }
            else:
                integrations[endpoint.endpoint] = {
                    CharmIntegration(
                        requirer=None,
                        requirer_endpoint=None,
                        provider=charm.name,
                        provider_endpoint=endpoint.endpoint,
                        interface=endpoint.interface,
                    )
                }

        all_required_integrations[charm.name] = integrations

    # Get all possible combinations of charms that can fulfill the non-optional interfaces
    all_possible_fulfillments: Dict[str, set[frozenset[str]]] = {}  # charm -> {{possible set of charms}}
    for charm, interfaces in all_required_integrations.items():
        all_possible_fulfillments[charm] = set()

        fulfilling_charms = set()
        for possible_integrations in interfaces.values():
            integration_fulfilling_charms = set()
            for integration in possible_integrations:
                if integration.provider and integration.provider != charm:
                    integration_fulfilling_charms.add(integration.provider)
                if integration.requirer and integration.requirer != charm:
                    integration_fulfilling_charms.add(integration.requirer)
            if len(integration_fulfilling_charms) > 0:
                fulfilling_charms.add(frozenset(integration_fulfilling_charms))

        all_possible_fulfillments[charm] = {frozenset(combo) for combo in itertools.product(*fulfilling_charms)}

    # For each charm, traverse the set of possible fulfillments to find the possible sets
    def get_dependencies(charm: str, existing_charms: set[str]) -> set[str]:
        # Check if charm already chosen
        if charm in existing_charms:
            return existing_charms

        # We must add charm
        existing_charms |= {charm}

        # Check each possible fulfillment
        additional_dependencies = []
        for fulfillment in all_possible_fulfillments[charm]:
            missing_charms = fulfillment - existing_charms
            sub_existing = existing_charms | fulfillment
            additional_dependencies.append(
                sub_existing
                | {
                    dependency
                    for missing_charm in missing_charms
                    for dependency in get_dependencies(missing_charm, sub_existing)
                }
            )

        # Return smallest additional dependencies
        if len(additional_dependencies) == 0:
            return existing_charms
        else:
            return existing_charms | min(additional_dependencies, key=lambda dependency_list: len(dependency_list))

    minimal_dependencies: Dict[str, set[str]] = {}  # charm -> required charms
    for charm in all_possible_fulfillments.keys():
        minimal_dependencies[charm] = get_dependencies(charm, set())

    # Fulfill charm integrations
    charm_to_integrations: Dict[str, Set[CharmIntegration]] = {}
    for deployed_charms in minimal_dependencies.values():
        all_integrations = set()
        for charm in deployed_charms:
            for required_integrations in all_required_integrations[charm].values():
                for integration in required_integrations:
                    if integration.provider in deployed_charms and integration.requirer in deployed_charms:
                        all_integrations.add(integration)
                        break
                    elif not integration.provider or not integration.requirer:
                        all_integrations.add(integration)
                        break
            charm_to_integrations[charm] = all_integrations

    return charm_to_integrations


def analyze_stats(charm_infos: set[CharmInfo], description: str, interfaces_in_catalog: set[str]):
    print("-------------------------")
    print(f"Statistics for {description}: (count: {len(charm_infos)})")

    # Analyze integration stats
    all_requires = {integration.interface for info in charm_infos for integration in info.requires}
    all_provides = {integration.interface for info in charm_infos for integration in info.provides}
    print(f"Total interfaces: {len(all_requires | all_provides)}")
    print(f"Total interfaces in catalog: {len((all_requires | all_provides) & interfaces_in_catalog)}")
    print(f"Total requires: {len(all_requires)}")
    print(f"Total requires in catalog: {len(all_requires & interfaces_in_catalog)}")
    print(f"Total provides: {len(all_provides)}")
    print(f"Total provides in catalog: {len(all_provides & interfaces_in_catalog)}")

    # Map integrations
    integrations = {
        CharmIntegration(
            requirer=requirer_info.name,
            requirer_endpoint=requirer_integration.endpoint,
            provider=provider_info.name,
            provider_endpoint=provider_integration.endpoint,
            interface=requirer_integration.interface,
        )
        for requirer_info in charm_infos
        for requirer_integration in requirer_info.requires
        for provider_info in charm_infos
        for provider_integration in provider_info.provides
        if requirer_integration.interface == provider_integration.interface
    }
    print(f"Total integrations: {len(integrations)}")
    interface_map = {
        interface: {integration for integration in integrations if integration.interface == interface}
        for interface in {integration.interface for integration in integrations}
    }
    for interface in sorted(all_requires | all_provides):
        print(f"    {interface}:")
        for integration in interface_map.get(interface, {}):
            print(
                f"        {integration.requirer}:{integration.requirer_endpoint} <-> {integration.provider}:{integration.provider_endpoint}"
            )
    print(
        f"Total integrations in catalog: {len({integration for integration in integrations if integration.interface in interfaces_in_catalog})}"
    )
    integrations_excluding_self = {
        integration for integration in integrations if integration.provider != integration.requirer
    }
    print(f"Total integrations excluding self integrations: {len(integrations_excluding_self)}")
    print(
        f"Total integrations excluding self integrations in catalog: {len({integration for integration in integrations_excluding_self if integration.interface in interfaces_in_catalog})}"
    )

    # Generate bundle pairings
    print("Bundle pairings:")
    bundle_pairings = find_bundle_pairings(charm_infos)
    bundle_pairings_charms = {}
    for charm, integrations in sorted(
        [(charm, integrations) for charm, integrations in bundle_pairings.items()], key=lambda v: v[0]
    ):
        missing_endpoints = []
        output_integrations = []
        unique_charms = set()
        for integration in integrations:
            if integration.provider and integration.requirer:
                output_integrations.append(
                    f"{integration.provider}:{integration.provider_endpoint} <-> {integration.requirer}:{integration.requirer_endpoint}"
                )
            else:
                missing_endpoints.append(integration.provider_endpoint or integration.requirer_endpoint)

            if integration.provider:
                unique_charms.add(integration.provider)
            if integration.requirer:
                unique_charms.add(integration.requirer)

        print(f"    {charm}: {unique_charms}")
        for output_integration in sorted(output_integrations):
            print(f"        {output_integration}")
        if len(missing_endpoints) > 0:
            print(f"        missing required endpoints: {', '.join(missing_endpoints)}")

        bundle_pairings_charms[charm] = unique_charms

    # Stats on bundle pairings
    largest_charm, largest_bundle_charms = max(
        [(charm, charms) for charm, charms in bundle_pairings_charms.items()], key=lambda v: len(v[1])
    )
    print(f"Largest bundle: {largest_charm}, {len(largest_bundle_charms)}, {largest_bundle_charms}")
    smallest_charm, smallest_bundle_charms = min(
        [(charm, charms) for charm, charms in bundle_pairings_charms.items()], key=lambda v: len(v[1])
    )
    print(f"Smallest bundle: {smallest_charm}, {len(smallest_bundle_charms)}, {smallest_bundle_charms}")
    average_bundle_charms = sum([len(charms) for charms in bundle_pairings_charms.values()]) / len(
        bundle_pairings_charms
    )
    print(f"Average bundle size: {average_bundle_charms}")


def analyze_all_stats(charm_infos: set[CharmInfo], filter: str, interfaces_in_catalog: set[str]):
    analyze_stats(charm_infos, f"all charms in {filter}", interfaces_in_catalog)
    analyze_stats(
        {info for info in charm_infos if info.has_k8s_api}, f"charms with k8s-api in {filter}", interfaces_in_catalog
    )
    analyze_stats(
        {info for info in charm_infos if info.has_deployable_on_kubernetes},
        f"charms with deployable_on kubernetes in {filter}",
        interfaces_in_catalog,
    )


def get_interfaces_in_catalog() -> set[str]:
    return {
        file["name"]
        for file in query_http("https://api.github.com/repos/canonical/charm-relation-interfaces/contents/interfaces")
    }


def main():
    # Get defined interfaces
    interfaces_in_catalog = get_interfaces_in_catalog()

    # Get all charms
    charms_with_risk = get_all_charms()
    print(f"Total number of charms: {len({charm_with_risk.name for charm_with_risk in charms_with_risk})}")

    # Get all versions
    charm_infos = {get_info(charm) for charm in {charm_with_risk.name for charm_with_risk in charms_with_risk}}

    # Analyze
    analyze_all_stats(charm_infos, "all risks", interfaces_in_catalog)
    analyze_all_stats(
        {info for info in charm_infos if {CharmWithRisk(name=info.name, risk="stable")} & charms_with_risk},
        "stable",
        interfaces_in_catalog,
    )
    analyze_all_stats(
        {info for info in charm_infos if {CharmWithRisk(name=info.name, risk="candidate")} & charms_with_risk},
        "candidate",
        interfaces_in_catalog,
    )
    analyze_all_stats(
        {
            info
            for info in charm_infos
            if {CharmWithRisk(name=info.name, risk="stable"), CharmWithRisk(name=info.name, risk="candidate")}
            & charms_with_risk
        },
        "candidate or stable",
        interfaces_in_catalog,
    )


# Run main when top-level environment
if __name__ == "__main__":
    main()
