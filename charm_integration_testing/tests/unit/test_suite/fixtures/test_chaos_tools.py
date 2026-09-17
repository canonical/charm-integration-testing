# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass
from inspect import unwrap
from pathlib import Path
from typing import cast

import pytest
from juju import JujuClient, JujuExtension, JujuModelHandle
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend
from test_suite.fixtures import chaos_tools
from test_suite.fixtures.chaos_tools import (
    CHAOS_MESH_CRDS,
    ChaosTool,
    detect_initial_tools,
    prepare_existing_model,
    require_tool_for_model,
    select_chaos_tool,
)
from test_suite.scheduler.states import STATES_WITHOUT_EXISTING_MODEL, State

from ...extensions.test_litmus import CONFIG, NEIGHBOR, TARGET, KubernetesStub, LitmusBackendStub, make_client


@dataclass
class ConfigStub:
    options: dict[str, str | float | None]

    def getoption(self, name: str) -> str | float | None:
        return self.options[name]


@dataclass
class RequestStub:
    config: ConfigStub


def make_request(**options: str | float | None) -> pytest.FixtureRequest:
    values: dict[str, str | float | None] = {
        "--litmus-offer": None,
        "--neighbor-litmus-offer": None,
        "--litmus-channel": "dev/edge",
        "--litmus-timeout": 600,
        "--current-state": State.NO_BUNDLE.value,
    }
    values.update({f"--{key.replace('_', '-')}": value for key, value in options.items()})
    return cast(pytest.FixtureRequest, RequestStub(ConfigStub(values)))


class TestSelectChaosTool:
    @dataclass(frozen=True)
    class Params:
        label: str
        litmus_crd: bool
        operator_ready: bool
        mesh_crds: tuple[str, ...]
        expected: ChaosTool | None

    test_cases = [
        Params(
            label="prefer-litmus",
            litmus_crd=True,
            operator_ready=True,
            mesh_crds=CHAOS_MESH_CRDS,
            expected=ChaosTool.LITMUS,
        ),
        Params(
            label="litmus-only",
            litmus_crd=True,
            operator_ready=True,
            mesh_crds=(),
            expected=ChaosTool.LITMUS,
        ),
        Params(
            label="unready-litmus-falls-back",
            litmus_crd=True,
            operator_ready=False,
            mesh_crds=CHAOS_MESH_CRDS,
            expected=ChaosTool.CHAOS_MESH,
        ),
        Params(
            label="mesh-only",
            litmus_crd=False,
            operator_ready=False,
            mesh_crds=CHAOS_MESH_CRDS,
            expected=ChaosTool.CHAOS_MESH,
        ),
        Params(label="neither", litmus_crd=False, operator_ready=False, mesh_crds=(), expected=None),
        Params(label="crd-without-operator", litmus_crd=True, operator_ready=False, mesh_crds=(), expected=None),
        Params(label="operator-without-crd", litmus_crd=False, operator_ready=True, mesh_crds=(), expected=None),
        Params(
            label="incomplete-mesh",
            litmus_crd=False,
            operator_ready=False,
            mesh_crds=CHAOS_MESH_CRDS[:1],
            expected=None,
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=lambda params: params.label)
    def test_selection(self, params: Params) -> None:
        # GIVEN the available resources in the target namespace
        backend = KubernetesStub()
        backend.crds.update(params.mesh_crds)
        if params.litmus_crd:
            backend.crds.add("chaosengines.litmuschaos.io")
        if params.operator_ready:
            backend.ready_namespaces.add(TARGET.model)

        # WHEN selecting a tool
        tool = select_chaos_tool(backend, TARGET.model)

        # THEN readiness and priority determine the selection
        assert tool == params.expected

    def test_availability_is_not_cached(self) -> None:
        # GIVEN an initially empty cluster
        backend = KubernetesStub()
        assert select_chaos_tool(backend, TARGET.model) is None

        # WHEN resources appear and disappear, THEN selection observes each change
        backend.crds.update(CHAOS_MESH_CRDS)
        assert select_chaos_tool(backend, TARGET.model) == ChaosTool.CHAOS_MESH
        backend.crds.add("chaosengines.litmuschaos.io")
        backend.ready_namespaces.add(TARGET.model)
        assert select_chaos_tool(backend, TARGET.model) == ChaosTool.LITMUS
        backend.ready_namespaces.clear()
        assert select_chaos_tool(backend, TARGET.model) == ChaosTool.CHAOS_MESH


class TestInitialDetection:
    def test_checks_each_scope_once_and_closes_clients(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN duplicate scopes and separate kubeconfigs
        backends: list[KubernetesStub] = []
        paths: list[Path] = []

        def factory(path: Path) -> KubernetesBackend:
            backend = KubernetesStub()
            backends.append(backend)
            paths.append(path)
            return backend

        # WHEN taking the session snapshot
        with caplog.at_level(logging.INFO):
            detect_initial_tools(
                [("target", "one"), ("target", "one"), ("neighbor", "two")],
                {"target": Path("target-config"), "neighbor": Path("neighbor-config")},
                logging.getLogger(__name__),
                backend_factory=factory,
            )

        # THEN each unique scope is checked and its client closed
        assert paths == [Path("target-config"), Path("neighbor-config")]
        assert all(backend.api_client.closed for backend in backends)
        assert "target/one: none" in caplog.text
        assert "neighbor/two: none" in caplog.text

    def test_missing_kubeconfig_is_not_reported_as_machine_cloud(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN no kubeconfig or known substrate
        def factory(path: Path) -> KubernetesBackend:
            pytest.fail("A client must not be created without a supplied kubeconfig")

        # WHEN taking the snapshot
        with caplog.at_level(logging.INFO):
            detect_initial_tools([("unknown", "model")], {}, logging.getLogger(__name__), backend_factory=factory)

        # THEN missing configuration is reported without classifying the cloud
        assert "not performed" in caplog.text
        assert "no kubeconfig supplied" in caplog.text

    def test_api_error_propagates_and_closes_client(self) -> None:
        # GIVEN an API permission error
        backend = KubernetesStub()
        backend.error = ApiException(status=403)

        # WHEN taking the snapshot, THEN the error is not reported as an absent tool
        with pytest.raises(ApiException) as exc_info:
            detect_initial_tools(
                [("target", TARGET.model)],
                {"target": Path("unused")},
                logging.getLogger(__name__),
                backend_factory=lambda path: backend,
            )
        assert exc_info.value is backend.error
        assert backend.api_client.closed


class TestRequireTool:
    def test_machine_model_skips_dependent_test(self) -> None:
        # GIVEN an authoritative machine-cloud result
        backend = LitmusBackendStub()
        backend.clients[TARGET.controller] = None

        # WHEN a test requires a tool, THEN only that request is skipped
        with pytest.raises(pytest.skip.Exception, match="Kubernetes model"):
            require_tool_for_model(backend, TARGET)

    def test_absent_tools_skip_dependent_test(self) -> None:
        # GIVEN neither tool in a Kubernetes model
        backend = LitmusBackendStub()

        # WHEN a test requires a tool, THEN absence produces a skip
        with pytest.raises(pytest.skip.Exception, match="Neither Litmus nor Chaos Mesh"):
            require_tool_for_model(backend, TARGET)

    @pytest.mark.parametrize("status", [401, 403, 500])
    def test_api_errors_are_not_skipped(self, status: int) -> None:
        # GIVEN available Mesh CRDs but an API failure
        backend = LitmusBackendStub()
        backend.kubernetes.crds.update(CHAOS_MESH_CRDS)
        backend.kubernetes.error = ApiException(status=status)

        # WHEN requiring a tool, THEN errors do not cause a skip or fallback
        with pytest.raises(ApiException) as exc_info:
            require_tool_for_model(backend, TARGET)
        assert exc_info.value is backend.kubernetes.error

    def test_missing_controller_configuration_is_not_skipped(self) -> None:
        # GIVEN a controller with no registered Kubernetes client
        backend = LitmusBackendStub()
        del backend.clients[TARGET.controller]

        # WHEN requiring a tool, THEN configuration failure propagates
        with pytest.raises(KeyError):
            require_tool_for_model(backend, TARGET)

    def test_configured_litmus_does_not_fall_back(self) -> None:
        # GIVEN Mesh availability but a missing configured Litmus relation
        backend = LitmusBackendStub()
        backend.kubernetes.crds.update(CHAOS_MESH_CRDS)
        resolve = unwrap(chaos_tools.chaos_tool_for_model)(backend, None, {TARGET: CONFIG})

        # WHEN requesting a tool, THEN configured setup failure remains visible
        with pytest.raises(RuntimeError, match="not connected"):
            resolve(TARGET)

    def test_configured_litmus_is_rechecked(self) -> None:
        # GIVEN a previously connected configured model
        backend = LitmusBackendStub()
        backend.install(TARGET)
        resolve = unwrap(chaos_tools.chaos_tool_for_model)(backend, None, {TARGET: CONFIG})
        assert resolve(TARGET) == ChaosTool.LITMUS

        # WHEN its relation disappears, THEN the next request fails
        backend.connected.remove(TARGET)
        with pytest.raises(RuntimeError, match="not connected"):
            resolve(TARGET)


class TestLitmusOptions:
    def test_neighbor_does_not_inherit_target_offer(self) -> None:
        # GIVEN a target offer without a neighbor offer
        request = make_request(litmus_offer=CONFIG.offer_url)

        # WHEN resolving configuration, THEN only the target is configured
        assert unwrap(chaos_tools.litmus_configs)(request, TARGET, NEIGHBOR) == {TARGET: CONFIG}

    def test_neighbor_offer_requires_neighbor_model(self) -> None:
        # GIVEN a neighbor offer without a neighbor model
        request = make_request(neighbor_litmus_offer=CONFIG.offer_url)

        # WHEN resolving configuration, THEN this is a usage error
        with pytest.raises(pytest.UsageError, match="requires a neighbor model"):
            unwrap(chaos_tools.litmus_configs)(request, TARGET, None)

    @pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
    def test_invalid_timeout_is_usage_error(self, timeout: float) -> None:
        # GIVEN an invalid timeout with Litmus enabled
        request = make_request(litmus_offer=CONFIG.offer_url, litmus_timeout=timeout)

        # WHEN resolving configuration, THEN it fails before deployment
        with pytest.raises(pytest.UsageError):
            unwrap(chaos_tools.litmus_configs)(request, TARGET, None)


class TestPrepareExistingModel:
    def test_does_not_run_unrelated_hooks(self, tmp_path: Path) -> None:
        # GIVEN a resumed model with unrelated workload hooks
        class UnexpectedHook(JujuExtension):
            def post_deploy(self, model: JujuModelHandle) -> None:
                pytest.fail("Infrastructure preparation must not run unrelated hooks")

        backend = LitmusBackendStub()
        client = JujuClient(backend, logging.getLogger(__name__), extensions=[UnexpectedHook()])

        # WHEN preparing the existing model
        prepare_existing_model(client, TARGET, CONFIG, tmp_path)

        # THEN infrastructure is connected and ready without recreating the model
        assert TARGET in backend.connected
        assert backend.created_models == []
        assert len(backend.deployments) == 1
        assert backend.kubernetes.reads == [(TARGET.model, "chaos-operator-ce")]

    def test_connected_model_is_not_redeployed(self, tmp_path: Path) -> None:
        # GIVEN an existing ready connection
        backend = LitmusBackendStub()
        backend.install(TARGET)

        # WHEN preparing the model again
        prepare_existing_model(make_client(backend, {TARGET: CONFIG}), TARGET, CONFIG, tmp_path)

        # THEN readiness is checked but no deployment is issued
        assert backend.deployments == []
        assert backend.kubernetes.reads == [(TARGET.model, "chaos-operator-ce")]

    @pytest.mark.parametrize("state", list(State), ids=lambda state: state.value)
    def test_startup_state_controls_preparation(self, state: State, tmp_path_factory: pytest.TempPathFactory) -> None:
        # GIVEN a fresh or resumed session
        backend = LitmusBackendStub()
        request = make_request(current_state=state.value)

        # WHEN the startup fixture prepares configured models
        unwrap(chaos_tools.prepare_litmus_models)(
            request, backend, logging.getLogger(__name__), {TARGET: CONFIG}, tmp_path_factory, None, None
        )

        # THEN only existing models are prepared at startup
        assert len(backend.deployments) == (0 if state in STATES_WITHOUT_EXISTING_MODEL else 1)
