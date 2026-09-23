# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from enum import Enum
from pathlib import Path
from typing import Callable

import pytest
from chaos_client.litmus_detection import litmus_is_available
from juju import JujuBackend, JujuModelHandle
from kubernetes_client import KubernetesBackend

from test_suite.scheduler.states import STATES_WITHOUT_EXISTING_MODEL, State

CHAOS_MESH_CRDS = ("stresschaos.chaos-mesh.org", "iochaos.chaos-mesh.org")


class ChaosTool(str, Enum):
    LITMUS = "litmus"
    CHAOS_MESH = "chaos-mesh"


def available_chaos_tools(backend: KubernetesBackend) -> frozenset[ChaosTool]:
    """Check current availability of every chaos tool, without choosing between them."""
    tools: set[ChaosTool] = set()
    if litmus_is_available(backend):
        tools.add(ChaosTool.LITMUS)
    # Check both CRDs so an absent one does not hide an API error for the other.
    mesh_crds_present = [backend.crd_exists(name) for name in CHAOS_MESH_CRDS]
    if all(mesh_crds_present):
        tools.add(ChaosTool.CHAOS_MESH)
    return frozenset(tools)


def preferred_chaos_tool(tools: frozenset[ChaosTool]) -> ChaosTool | None:
    """Pick Litmus over Chaos Mesh when both support the caller's need."""
    if ChaosTool.LITMUS in tools:
        return ChaosTool.LITMUS
    if ChaosTool.CHAOS_MESH in tools:
        return ChaosTool.CHAOS_MESH
    return None


def _format_tools(tools: frozenset[ChaosTool]) -> str:
    return ", ".join(sorted(tool.value for tool in tools)) or "none"


def detect_initial_tools(
    scopes: list[tuple[str, str]],
    kubeconfigs: dict[str, Path],
    logger: logging.Logger,
    *,
    backend_factory: Callable[[Path], KubernetesBackend] = KubernetesBackend.k8s_client,
) -> None:
    """Report a session-start snapshot without requiring a test controller to exist."""
    for cloud, namespace in dict.fromkeys(scopes):
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
        for model in {model.uri: model for model in models}.values():
            kubernetes = juju_backend.get_kubernetes_client_for_model(model)
            if kubernetes is None:
                logger.info("Chaos detection not performed for model %s: non-Kubernetes model.", model.uri)
                continue
            # The backend owns this client and may reuse it after the snapshot.
            tools = available_chaos_tools(kubernetes.backend)
            logger.info("Initial chaos tools for %s: %s.", model.uri, _format_tools(tools))
        return
    scopes = [(target_cloud, target_model_ref.model)]
    if neighbor_cloud is not None and neighbor_model_ref is not None:
        scopes.append((neighbor_cloud, neighbor_model_ref.model))
    detect_initial_tools(scopes, cloud_kubeconfigs, logger)


def require_tool_for_model(backend: JujuBackend, model: JujuModelHandle) -> ChaosTool:
    """Skip tool-dependent tests only for unsupported substrates or unavailable tools."""
    kubernetes = backend.get_kubernetes_client_for_model(model)
    if kubernetes is None:
        pytest.skip("Litmus and Chaos Mesh require a Kubernetes model.")
    tool = preferred_chaos_tool(available_chaos_tools(kubernetes.backend))
    if tool is None:
        pytest.skip(f"Neither Litmus nor Chaos Mesh is available for {model.uri}.")
    return tool


@pytest.fixture
def chaos_tool_for_model(
    juju_backend: JujuBackend, detect_chaos_tools: None
) -> Callable[[JujuModelHandle], ChaosTool]:
    """Resolve the available chaos tool for a test model."""

    def resolve(model: JujuModelHandle) -> ChaosTool:
        return require_tool_for_model(juju_backend, model)

    return resolve


@pytest.fixture
def require_chaos_tool(
    chaos_tool_for_model: Callable[[JujuModelHandle], ChaosTool], target_model_ref: JujuModelHandle
) -> ChaosTool:
    return chaos_tool_for_model(target_model_ref)
