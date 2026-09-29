# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from enum import Enum
from pathlib import Path
from typing import Callable, NoReturn

import pytest
from chaos_client import ChaosClient, ChaosMeshChaosClient, MetaChaosClient
from chaos_client.adapters import DiskFillClient, NetworkIsolationClient
from chaos_client.chaos_mesh_detection import chaos_mesh_is_available
from chaos_client.litmus_client import LitmusChaosClient
from chaos_client.litmus_detection import litmus_is_available
from juju import JujuBackend, JujuModelHandle
from kubernetes_client import KubernetesBackend

from test_suite.scheduler.states import STATES_WITHOUT_EXISTING_MODEL, State


class ChaosTool(str, Enum):
    LITMUS = "litmus"
    CHAOS_MESH = "chaos-mesh"


def available_chaos_tools(backend: KubernetesBackend) -> frozenset[ChaosTool]:
    """Check current availability of every chaos tool, without choosing between them."""
    tools: set[ChaosTool] = set()
    if litmus_is_available(backend):
        tools.add(ChaosTool.LITMUS)
    if chaos_mesh_is_available(backend):
        tools.add(ChaosTool.CHAOS_MESH)
    return frozenset(tools)


def _format_tools(tools: frozenset[ChaosTool]) -> str:
    return ", ".join(sorted(tool.value for tool in tools)) or "none"


def detect_initial_tools(
    scopes: list[tuple[str, str]],
    kubeconfigs: dict[str, Path],
    logger: logging.Logger,
    *,
    backend_factory: Callable[[Path], KubernetesBackend] = KubernetesBackend.k8s_client,
) -> None:
    """Report tool availability once per cloud without requiring a test controller."""
    scopes_by_cloud: dict[str, str] = {}
    for cloud, namespace in scopes:
        scopes_by_cloud.setdefault(cloud, namespace)
    for cloud, namespace in scopes_by_cloud.items():
        path = kubeconfigs.get(cloud)
        if path is None:
            logger.info("Chaos detection not performed for cloud %s: no kubeconfig supplied.", cloud)
            continue
        backend = backend_factory(path)
        try:
            tools = available_chaos_tools(backend)
            logger.info("Initial chaos tools for %s/%s: %s.", cloud, namespace, _format_tools(tools))
        finally:
            backend.api_client.close()


@pytest.fixture(scope="session")
def detect_chaos_tools(
    request: pytest.FixtureRequest,
    juju_backend: JujuBackend,
    cloud_kubeconfigs: dict[str, Path],
    target_cloud: str,
    target_model_ref: JujuModelHandle,
    neighbor_cloud: str | None,
    neighbor_model_ref: JujuModelHandle | None,
    logger: logging.Logger,
    register_preexisting_resources: None,
) -> None:
    """Use live model clouds when models exist, configured clouds before creation."""
    if State(request.config.getoption("--current-state")) not in STATES_WITHOUT_EXISTING_MODEL:
        models = [target_model_ref]
        if neighbor_model_ref is not None:
            models.append(neighbor_model_ref)
        checked_clients: set[int] = set()
        for model in {model.uri: model for model in models}.values():
            kubernetes = juju_backend.get_kubernetes_client_for_model(model)
            if kubernetes is None:
                logger.info("Chaos detection not performed for model %s: non-Kubernetes model.", model.uri)
                continue
            if id(kubernetes) in checked_clients:
                continue
            checked_clients.add(id(kubernetes))
            # The backend owns this client and may reuse it after the snapshot.
            tools = available_chaos_tools(kubernetes.backend)
            logger.info("Initial chaos tools for %s: %s.", model.uri, _format_tools(tools))
        return
    scopes = [(target_cloud, target_model_ref.model)]
    if neighbor_cloud is not None and neighbor_model_ref is not None:
        scopes.append((neighbor_cloud, neighbor_model_ref.model))
    detect_initial_tools(scopes, cloud_kubeconfigs, logger)


def chaos_client_for_model(backend: JujuBackend, model: JujuModelHandle) -> MetaChaosClient:
    """Build experiment clients for the model's substrate."""
    tools: list[ChaosClient] = []
    kubernetes = backend.get_kubernetes_client_for_model(model)
    if kubernetes is not None:
        if litmus_is_available(kubernetes.backend):
            tools.append(LitmusChaosClient(kubernetes.backend))
        if chaos_mesh_is_available(kubernetes.backend):
            tools.append(ChaosMeshChaosClient(kubernetes.backend))
        tools.append(NetworkIsolationClient(kubernetes.backend))
    tools.append(DiskFillClient(backend))

    def skip_unsupported(operation: str) -> NoReturn:
        pytest.skip(f"No available chaos client supports '{operation}' for {model.uri}.")

    return MetaChaosClient(tools, on_unsupported=skip_unsupported)


@pytest.fixture
def chaos_tool_for_model(
    request: pytest.FixtureRequest, juju_backend: JujuBackend, detect_chaos_tools: None
) -> Callable[[JujuModelHandle], MetaChaosClient]:
    """Provide experiment clients with cleanup at test teardown."""

    def resolve(model: JujuModelHandle) -> MetaChaosClient:
        client = chaos_client_for_model(juju_backend, model)
        request.addfinalizer(client.cleanup_all)
        return client

    return resolve


@pytest.fixture
def require_chaos_tool(
    chaos_tool_for_model: Callable[[JujuModelHandle], MetaChaosClient], target_model_ref: JujuModelHandle
) -> MetaChaosClient:
    return chaos_tool_for_model(target_model_ref)
