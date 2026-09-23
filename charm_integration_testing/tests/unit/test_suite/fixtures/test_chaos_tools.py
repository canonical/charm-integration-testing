# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass
from inspect import unwrap
from pathlib import Path
from typing import cast

import pytest
from chaos_client.litmus_detection import LITMUS_CRDS, OPERATOR_NAMESPACE
from juju import JujuModelHandle
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend, KubernetesClient
from test_suite.fixtures import chaos_tools
from test_suite.fixtures.chaos_tools import (
    CHAOS_MESH_CRDS,
    ChaosTool,
    detect_initial_tools,
    require_tool_for_model,
    select_chaos_tool,
)
from test_suite.scheduler.states import STATES_WITHOUT_EXISTING_MODEL, State

from ...extensions.shared import NullJujuBackend

TARGET = JujuModelHandle(controller="target-controller", model="target-model")
NEIGHBOR = JujuModelHandle(controller="neighbor-controller", model="neighbor-model")


@dataclass
class ClosingApiStub:
    closed: bool = False

    def close(self) -> None:
        self.closed = True


class KubernetesStub(KubernetesBackend):
    def __init__(self) -> None:
        self.api_client = ClosingApiStub()
        self.crds: set[str] = set()
        self.ready_deployments: set[tuple[str, str]] = set()
        self.reads: list[tuple[str, str]] = []
        self.error: ApiException | None = None

    def crd_exists(self, name: str) -> bool:
        if self.error is not None:
            raise self.error
        return name in self.crds

    def deployment_is_ready(self, namespace: str, name: str) -> bool:
        self.reads.append((namespace, name))
        return (namespace, name) in self.ready_deployments


class JujuBackendStub(NullJujuBackend):
    def __init__(self) -> None:
        self.kubernetes = KubernetesStub()
        self.clients: dict[str, KubernetesClient | None] = {
            TARGET.uri: KubernetesClient(self.kubernetes),
            NEIGHBOR.uri: KubernetesClient(self.kubernetes),
        }
        self.resolutions: list[str] = []

    def get_kubernetes_client_for_model(self, model: JujuModelHandle) -> KubernetesClient | None:
        self.resolutions.append(model.uri)
        return self.clients[model.uri]


@dataclass
class ConfigStub:
    options: dict[str, str]

    def getoption(self, name: str) -> str:
        return self.options[name]


@dataclass
class RequestStub:
    config: ConfigStub


def make_request(**options: str) -> pytest.FixtureRequest:
    values: dict[str, str] = {
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
        # GIVEN the available CRDs and shared operator
        backend = KubernetesStub()
        backend.crds.update(params.mesh_crds)
        if params.litmus_crd:
            backend.crds.update(LITMUS_CRDS)
        if params.operator_ready:
            backend.ready_deployments.add((OPERATOR_NAMESPACE, "litmus"))

        # WHEN selecting a tool
        tool = select_chaos_tool(backend)

        # THEN readiness and priority determine the selection
        assert tool == params.expected

    @pytest.mark.parametrize("status", [401, 403, 500])
    def test_second_crd_error_propagates_when_first_is_absent(self, status: int) -> None:
        # GIVEN absent Litmus and StressChaos CRDs, and an error reading IOChaos
        error = ApiException(status=status)
        reads: list[str] = []

        class CrdErrorStub(KubernetesStub):
            def crd_exists(self, name: str) -> bool:
                reads.append(name)
                if name == "iochaos.chaos-mesh.org":
                    raise error
                return False

        backend = CrdErrorStub()

        # WHEN selecting a tool, THEN the API error is not reported as absence
        with pytest.raises(ApiException) as exc_info:
            select_chaos_tool(backend)

        assert exc_info.value is error
        assert reads == [*LITMUS_CRDS, *CHAOS_MESH_CRDS]

    def test_availability_is_not_cached(self) -> None:
        # GIVEN an initially empty cluster
        backend = KubernetesStub()
        assert select_chaos_tool(backend) is None

        # WHEN resources appear and disappear, THEN selection observes each change
        backend.crds.update(CHAOS_MESH_CRDS)
        assert select_chaos_tool(backend) == ChaosTool.CHAOS_MESH
        backend.crds.update(LITMUS_CRDS)
        backend.ready_deployments.add((OPERATOR_NAMESPACE, "litmus"))
        assert select_chaos_tool(backend) == ChaosTool.LITMUS
        backend.ready_deployments.clear()
        assert select_chaos_tool(backend) == ChaosTool.CHAOS_MESH


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


class TestInitialDetectionFixture:
    @pytest.mark.parametrize("state", list(State), ids=lambda state: state.value)
    def test_state_selects_model_or_configured_cloud(self, state: State, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN configured controller clouds without kubeconfigs and live model clients
        backend = JujuBackendStub()
        backend.kubernetes.crds.update(CHAOS_MESH_CRDS)
        neighbor = KubernetesStub()
        neighbor.crds.update(LITMUS_CRDS)
        neighbor.ready_deployments.add((OPERATOR_NAMESPACE, "litmus"))
        backend.clients[NEIGHBOR.uri] = KubernetesClient(neighbor)

        # WHEN taking the initial snapshot for a fresh or resumed session
        with caplog.at_level(logging.INFO):
            unwrap(chaos_tools.detect_chaos_tools)(
                make_request(current_state=state.value),
                backend,
                {},
                "target-controller-cloud",
                TARGET,
                "neighbor-controller-cloud",
                NEIGHBOR,
                logging.getLogger(__name__),
                None,
            )

        # THEN only pre-model states use the configured clouds without querying Juju
        if state in STATES_WITHOUT_EXISTING_MODEL:
            assert backend.resolutions == []
            assert "cloud target-controller-cloud: no kubeconfig supplied" in caplog.text
            assert "cloud neighbor-controller-cloud: no kubeconfig supplied" in caplog.text
        else:
            assert backend.resolutions == [TARGET.uri, NEIGHBOR.uri]
            assert f"{TARGET.uri}: chaos-mesh" in caplog.text
            assert f"{NEIGHBOR.uri}: litmus" in caplog.text
            assert "controller-cloud" not in caplog.text
        assert not backend.kubernetes.api_client.closed
        assert not neighbor.api_client.closed

    def test_duplicate_model_is_checked_once(self) -> None:
        # GIVEN the same model configured as target and neighbor
        backend = JujuBackendStub()

        # WHEN taking the snapshot
        unwrap(chaos_tools.detect_chaos_tools)(
            make_request(current_state=State.DEPLOYED.value),
            backend,
            {},
            "unused",
            TARGET,
            "unused",
            TARGET,
            logging.getLogger(__name__),
            None,
        )

        # THEN the model is resolved only once
        assert backend.resolutions == [TARGET.uri]

    def test_machine_model_is_logged_without_skipping_session(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN an existing machine model
        backend = JujuBackendStub()
        backend.clients[TARGET.uri] = None

        # WHEN taking the snapshot
        with caplog.at_level(logging.INFO):
            unwrap(chaos_tools.detect_chaos_tools)(
                make_request(current_state=State.DEPLOYED.value),
                backend,
                {},
                "unused",
                TARGET,
                None,
                None,
                logging.getLogger(__name__),
                None,
            )

        # THEN the substrate is reported without skipping the session
        assert f"model {TARGET.uri}: non-Kubernetes model" in caplog.text
        assert backend.resolutions == [TARGET.uri]

    def test_model_resolution_error_does_not_fall_back(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN a model lookup or configuration failure
        class FailingBackend(JujuBackendStub):
            def get_kubernetes_client_for_model(self, model: JujuModelHandle) -> KubernetesClient | None:
                raise error

        error = RuntimeError("Model lookup failed")
        backend = FailingBackend()

        # WHEN taking the snapshot, THEN the original error propagates
        with pytest.raises(RuntimeError) as exc_info:
            unwrap(chaos_tools.detect_chaos_tools)(
                make_request(current_state=State.DEPLOYED.value),
                backend,
                {},
                "unused",
                TARGET,
                None,
                None,
                logging.getLogger(__name__),
                None,
            )
        assert exc_info.value is error
        assert "not performed" not in caplog.text

    @pytest.mark.parametrize("status", [401, 403, 500])
    def test_api_error_propagates_without_closing_shared_client(self, status: int) -> None:
        # GIVEN an API error from a backend-owned Kubernetes client
        backend = JujuBackendStub()
        backend.kubernetes.error = ApiException(status=status)

        # WHEN taking the snapshot, THEN the error propagates and ownership is preserved
        with pytest.raises(ApiException) as exc_info:
            unwrap(chaos_tools.detect_chaos_tools)(
                make_request(current_state=State.DEPLOYED.value),
                backend,
                {},
                "unused",
                TARGET,
                None,
                None,
                logging.getLogger(__name__),
                None,
            )
        assert exc_info.value is backend.kubernetes.error
        assert not backend.kubernetes.api_client.closed


class TestRequireTool:
    def test_machine_model_skips_dependent_test(self) -> None:
        # GIVEN an authoritative machine-cloud result
        backend = JujuBackendStub()
        backend.clients[TARGET.uri] = None

        # WHEN a test requires a tool, THEN only that request is skipped
        with pytest.raises(pytest.skip.Exception, match="Kubernetes model"):
            require_tool_for_model(backend, TARGET)

    def test_absent_tools_skip_dependent_test(self) -> None:
        # GIVEN neither tool in a Kubernetes model
        backend = JujuBackendStub()

        # WHEN a test requires a tool, THEN absence produces a skip
        with pytest.raises(pytest.skip.Exception, match="Neither Litmus nor Chaos Mesh"):
            require_tool_for_model(backend, TARGET)

    @pytest.mark.parametrize("status", [401, 403, 500])
    def test_api_errors_are_not_skipped(self, status: int) -> None:
        # GIVEN available Mesh CRDs but an API failure
        backend = JujuBackendStub()
        backend.kubernetes.crds.update(CHAOS_MESH_CRDS)
        backend.kubernetes.error = ApiException(status=status)

        # WHEN requiring a tool, THEN errors do not cause a skip or fallback
        with pytest.raises(ApiException) as exc_info:
            require_tool_for_model(backend, TARGET)
        assert exc_info.value is backend.kubernetes.error

    def test_missing_model_configuration_is_not_skipped(self) -> None:
        # GIVEN a model with no registered Kubernetes client
        backend = JujuBackendStub()
        del backend.clients[TARGET.uri]

        # WHEN requiring a tool, THEN configuration failure propagates
        with pytest.raises(KeyError):
            require_tool_for_model(backend, TARGET)

    def test_shared_operator_serves_both_models(self) -> None:
        # GIVEN two models sharing a cluster with Litmus and Chaos Mesh
        backend = JujuBackendStub()
        backend.kubernetes.crds.update((*LITMUS_CRDS, *CHAOS_MESH_CRDS))
        backend.kubernetes.ready_deployments.add((OPERATOR_NAMESPACE, "litmus"))
        resolve = unwrap(chaos_tools.chaos_tool_for_model)(backend)

        # WHEN both models request a chaos tool
        target_tool = resolve(TARGET)
        neighbor_tool = resolve(NEIGHBOR)

        # THEN both use the shared operator without querying charm configuration
        assert target_tool == neighbor_tool == ChaosTool.LITMUS
        assert backend.kubernetes.reads == [("litmus-system", "litmus")] * 2
        assert backend.resolutions == [TARGET.uri, NEIGHBOR.uri]

    def test_selection_is_rechecked_for_each_request(self) -> None:
        # GIVEN a shared operator and an available Chaos Mesh installation
        backend = JujuBackendStub()
        backend.kubernetes.crds.update((*LITMUS_CRDS, *CHAOS_MESH_CRDS))
        backend.kubernetes.ready_deployments.add((OPERATOR_NAMESPACE, "litmus"))
        resolve = unwrap(chaos_tools.chaos_tool_for_model)(backend)
        assert resolve(TARGET) == ChaosTool.LITMUS

        # WHEN the shared operator stops
        backend.kubernetes.ready_deployments.clear()

        # THEN the next request falls back to Chaos Mesh
        assert resolve(TARGET) == ChaosTool.CHAOS_MESH

    def test_target_fixture_uses_target_model(self) -> None:
        # GIVEN a tool resolver that records the requested model
        models: list[JujuModelHandle] = []

        def resolve(model: JujuModelHandle) -> ChaosTool:
            models.append(model)
            return ChaosTool.LITMUS

        # WHEN the target test requires a chaos tool
        tool = unwrap(chaos_tools.require_chaos_tool)(resolve, TARGET)

        # THEN the target model's tool is returned
        assert tool == ChaosTool.LITMUS
        assert models == [TARGET]
