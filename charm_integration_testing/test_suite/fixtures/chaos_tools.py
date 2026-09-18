# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Callable

import pytest
from chaos_client.litmus_detection import litmus_is_available
from extensions.litmus.extension import (
    DEFAULT_LITMUS_CHANNEL,
    DEFAULT_LITMUS_TIMEOUT,
    LitmusConfig,
    LitmusExtension,
    infrastructure_bundle,
    infrastructure_is_connected,
    validate_litmus_target,
    wait_for_litmus,
)
from juju import JujuBackend, JujuClient, JujuModelHandle
from kubernetes_client import KubernetesBackend

from test_suite.scheduler.states import STATES_WITHOUT_EXISTING_MODEL, State

CHAOS_MESH_CRDS = ("stresschaos.chaos-mesh.org", "iochaos.chaos-mesh.org")


class ChaosTool(str, Enum):
    LITMUS = "litmus"
    CHAOS_MESH = "chaos-mesh"


def select_chaos_tool(backend: KubernetesBackend, namespace: str) -> ChaosTool | None:
    """Check current availability, preferring Litmus over Chaos Mesh."""
    if litmus_is_available(backend, namespace):
        return ChaosTool.LITMUS
    if all(backend.crd_exists(name) for name in CHAOS_MESH_CRDS):
        return ChaosTool.CHAOS_MESH
    return None


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("chaos tools")
    group.addoption(
        "--litmus-offer",
        default=None,
        help="ChaosCenter controller:owner/model.offer for the target model on the same K8s cluster.",
    )
    group.addoption(
        "--neighbor-litmus-offer",
        default=None,
        help="ChaosCenter offer for the neighbor model; not inherited from --litmus-offer.",
    )
    group.addoption("--litmus-channel", default=DEFAULT_LITMUS_CHANNEL, help="Infrastructure charm channel.")
    group.addoption(
        "--litmus-timeout",
        type=float,
        default=DEFAULT_LITMUS_TIMEOUT.total_seconds(),
        help="Seconds to wait for Litmus infrastructure readiness.",
    )


@pytest.fixture(scope="session")
def litmus_configs(
    request: pytest.FixtureRequest,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
) -> dict[JujuModelHandle, LitmusConfig]:
    """Cache configuration only, not resource availability."""
    configs: dict[JujuModelHandle, LitmusConfig] = {}
    for option, model in (("--litmus-offer", target_model_ref), ("--neighbor-litmus-offer", neighbor_model_ref)):
        offer = request.config.getoption(option)
        if offer is None:
            continue
        if model is None:
            raise pytest.UsageError(f"{option} requires a neighbor model.")
        try:
            configs[model] = LitmusConfig(
                offer_url=offer,
                channel=request.config.getoption("--litmus-channel"),
                timeout=timedelta(seconds=request.config.getoption("--litmus-timeout")),
            )
        except (ValueError, OverflowError) as error:
            raise pytest.UsageError(str(error)) from error
    return configs


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
            tool = select_chaos_tool(backend, namespace)
            logger.info("Initial chaos tool for %s/%s: %s.", cloud, namespace, tool.value if tool else "none")
        finally:
            backend.api_client.close()


@pytest.fixture(scope="session", autouse=True)
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
            tool = select_chaos_tool(kubernetes.backend, model.model)
            logger.info("Initial chaos tool for %s: %s.", model.uri, tool.value if tool else "none")
        return
    scopes = [(target_cloud, target_model_ref.model)]
    if neighbor_cloud is not None and neighbor_model_ref is not None:
        scopes.append((neighbor_cloud, neighbor_model_ref.model))
    detect_initial_tools(scopes, cloud_kubeconfigs, logger)


def prepare_existing_model(client: JujuClient, model: JujuModelHandle, config: LitmusConfig, directory: Path) -> None:
    """Connect a resumed model to Litmus and wait for readiness."""
    kubernetes = validate_litmus_target(client.backend, model, config)
    if infrastructure_is_connected(client.backend, model, config):
        wait_for_litmus(kubernetes, model.model, config.timeout)
        return
    path = directory / "litmus-bundle.yaml"
    path.write_text(infrastructure_bundle(config), encoding="utf-8")
    # Infrastructure-only deployment must not reinject validators or run workload hooks.
    litmus_client = JujuClient(
        client.backend, client.logger, extensions=[LitmusExtension(client.backend, {model: config})]
    )
    litmus_client.deploy_bundle_file(str(path), model)


@pytest.fixture(scope="session", autouse=True)
def prepare_litmus_models(
    request: pytest.FixtureRequest,
    juju_backend: JujuBackend,
    logger: logging.Logger,
    litmus_configs: dict[JujuModelHandle, LitmusConfig],
    tmp_path_factory: pytest.TempPathFactory,
    register_preexisting_resources: None,
    detect_chaos_tools: None,
) -> None:
    if not litmus_configs or State(request.config.getoption("--current-state")) in STATES_WITHOUT_EXISTING_MODEL:
        return
    client = JujuClient(juju_backend, logger)
    directory = tmp_path_factory.mktemp("litmus")
    for model, config in litmus_configs.items():
        prepare_existing_model(client, model, config, directory)


def require_tool_for_model(backend: JujuBackend, model: JujuModelHandle) -> ChaosTool:
    """Skip tool-dependent tests only for unsupported substrates or unavailable tools."""
    kubernetes = backend.get_kubernetes_client_for_model(model)
    if kubernetes is None:
        pytest.skip("Litmus and Chaos Mesh require a Kubernetes model.")
    tool = select_chaos_tool(kubernetes.backend, model.model)
    if tool is None:
        pytest.skip(f"Neither Litmus nor Chaos Mesh is available for {model.uri}.")
    return tool


@pytest.fixture
def chaos_tool_for_model(
    juju_backend: JujuBackend,
    prepare_litmus_models: None,
    litmus_configs: dict[JujuModelHandle, LitmusConfig],
) -> Callable[[JujuModelHandle], ChaosTool]:
    def resolve(model: JujuModelHandle) -> ChaosTool:
        config = litmus_configs.get(model)
        if config is not None:
            kubernetes = validate_litmus_target(juju_backend, model, config)
            if not infrastructure_is_connected(juju_backend, model, config):
                raise RuntimeError(f"Configured Litmus infrastructure is not connected in {model.uri}.")
            wait_for_litmus(kubernetes, model.model, config.timeout)
            return ChaosTool.LITMUS
        return require_tool_for_model(juju_backend, model)

    return resolve


@pytest.fixture
def require_chaos_tool(
    chaos_tool_for_model: Callable[[JujuModelHandle], ChaosTool], target_model_ref: JujuModelHandle
) -> ChaosTool:
    return chaos_tool_for_model(target_model_ref)
