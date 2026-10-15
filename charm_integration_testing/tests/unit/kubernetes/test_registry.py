# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from kubernetes_client import KubernetesClient, KubernetesClientRegistry, KubernetesExtension


class ExtensionStub(KubernetesExtension):
    pass


def test_clients_are_created_once_per_cloud() -> None:
    # GIVEN two configured clouds and one factory result for each
    configs = {"target": Path("target.yaml"), "neighbor": Path("neighbor.yaml")}
    clients = {cloud: MagicMock(spec=KubernetesClient) for cloud in configs}
    calls: list[Path] = []

    def factory(kubeconfig: Path) -> KubernetesClient:
        calls.append(kubeconfig)
        return clients[next(cloud for cloud, path in configs.items() if path == kubeconfig)]

    registry = KubernetesClientRegistry(configs, client_factory=factory)

    # WHEN resolving clients more than once
    first = registry.client_for_cloud("target")
    second = registry.client_for_cloud("target")
    neighbor = registry.client_for_cloud("neighbor")

    # THEN each cloud owns one reusable client
    assert first is second is clients["target"]
    assert neighbor is clients["neighbor"]
    assert calls == [Path("target.yaml"), Path("neighbor.yaml")]
    assert first.juju_cloud == "target"
    assert neighbor.juju_cloud == "neighbor"


def test_extensions_are_registered_on_existing_and_future_clients() -> None:
    # GIVEN a registry with an already-created client
    clients = {
        "target": MagicMock(spec=KubernetesClient),
        "neighbor": MagicMock(spec=KubernetesClient),
    }
    registry = KubernetesClientRegistry(
        {"target": Path("target.yaml"), "neighbor": Path("neighbor.yaml")},
        client_factory=lambda kubeconfig: clients[kubeconfig.stem],
    )
    target_client = registry.client_for_cloud("target")
    extension = ExtensionStub()

    # WHEN registering an extension and resolving another client
    registry.register_extension(extension)
    neighbor_client = registry.client_for_cloud("neighbor")

    # THEN it is attached to current and subsequently-created clients exactly once
    cast(MagicMock, target_client.register_extension).assert_called_once_with(extension)
    cast(MagicMock, neighbor_client.register_extension).assert_called_once_with(extension)


def test_constructor_extensions_are_registered_on_created_clients() -> None:
    # GIVEN an extension supplied when constructing the registry
    client = MagicMock(spec=KubernetesClient)
    extension = ExtensionStub()
    registry = KubernetesClientRegistry(
        {"target": Path("target.yaml")},
        client_factory=lambda _: client,
        extensions=[extension],
    )

    # WHEN a cloud client is created
    registry.client_for_cloud("target")

    # THEN it receives the registered extension and cloud context
    cast(MagicMock, client.register_extension).assert_called_once_with(extension)
    assert client.juju_cloud == "target"


def test_missing_cloud_configuration_is_reported() -> None:
    registry = KubernetesClientRegistry({})

    with pytest.raises(ValueError, match="No kubeconfig configured for Kubernetes cloud 'missing'"):
        registry.client_for_cloud("missing")
