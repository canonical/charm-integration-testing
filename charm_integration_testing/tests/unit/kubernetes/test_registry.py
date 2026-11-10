# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from kubernetes_client import KubernetesClient, KubernetesClientRegistry


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


def test_missing_cloud_configuration_is_reported() -> None:
    registry = KubernetesClientRegistry({})

    with pytest.raises(ValueError, match="No kubeconfig configured for Kubernetes cloud 'missing'"):
        registry.client_for_cloud("missing")
