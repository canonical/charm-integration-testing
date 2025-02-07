#!/usr/bin/env python
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import requests
import yaml
from pydantic.dataclasses import dataclass

CHARMHUB_API = "https://api.charmhub.io"


def query_http(url: str, params: dict | None = None) -> list | dict:
    response = requests.get(url, params=params)
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

    @classmethod
    def from_metadata(cls, metadata: dict) -> set:
        return {
            cls(
                endpoint=endpoint,
                interface=integration["interface"],
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
    requirer: str
    requirer_endpoint: str
    provider: str
    provider_endpoint: str
    interface: str


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
    for interface in all_requires | all_provides:
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
