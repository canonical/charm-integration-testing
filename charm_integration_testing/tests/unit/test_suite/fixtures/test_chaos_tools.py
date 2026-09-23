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
    available_chaos_tools,
    detect_initial_tools,
    preferred_chaos_tool,
    require_tools_for_model,
)
from test_suite.scheduler.states import STATES_WITHOUT_EXISTING_MODEL, State

from ...extensions.shared import NullJujuBackend

pytest_plugins = ["pytester"]

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


class TestAvailableChaosTools:
    @dataclass(frozen=True)
    class Params:
        label: str
        litmus_crd: bool
        operator_ready: bool
        mesh_crds: tuple[str, ...]
        expected: frozenset[ChaosTool]

    test_cases = [
        Params(
            label="both-available",
            litmus_crd=True,
            operator_ready=True,
            mesh_crds=CHAOS_MESH_CRDS,
            expected=frozenset({ChaosTool.LITMUS, ChaosTool.CHAOS_MESH}),
        ),
        Params(
            label="litmus-only",
            litmus_crd=True,
            operator_ready=True,
            mesh_crds=(),
            expected=frozenset({ChaosTool.LITMUS}),
        ),
        Params(
            label="unready-litmus-leaves-mesh",
            litmus_crd=True,
            operator_ready=False,
            mesh_crds=CHAOS_MESH_CRDS,
            expected=frozenset({ChaosTool.CHAOS_MESH}),
        ),
        Params(
            label="mesh-only",
            litmus_crd=False,
            operator_ready=False,
            mesh_crds=CHAOS_MESH_CRDS,
            expected=frozenset({ChaosTool.CHAOS_MESH}),
        ),
        Params(label="neither", litmus_crd=False, operator_ready=False, mesh_crds=(), expected=frozenset()),
        Params(
            label="crd-without-operator",
            litmus_crd=True,
            operator_ready=False,
            mesh_crds=(),
            expected=frozenset(),
        ),
        Params(
            label="operator-without-crd",
            litmus_crd=False,
            operator_ready=True,
            mesh_crds=(),
            expected=frozenset(),
        ),
        Params(
            label="incomplete-mesh",
            litmus_crd=False,
            operator_ready=False,
            mesh_crds=CHAOS_MESH_CRDS[:1],
            expected=frozenset(),
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=lambda params: params.label)
    def test_detection(self, params: Params) -> None:
        # GIVEN the available CRDs and shared operator
        backend = KubernetesStub()
        backend.crds.update(params.mesh_crds)
        if params.litmus_crd:
            backend.crds.update(LITMUS_CRDS)
        if params.operator_ready:
            backend.ready_deployments.add((OPERATOR_NAMESPACE, "litmus"))

        # WHEN checking availability
        tools = available_chaos_tools(backend)

        # THEN readiness determines which tools are reported
        assert tools == params.expected

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

        # WHEN checking availability, THEN the API error is not reported as absence
        with pytest.raises(ApiException) as exc_info:
            available_chaos_tools(backend)

        assert exc_info.value is error
        assert reads == [*LITMUS_CRDS, *CHAOS_MESH_CRDS]

    def test_availability_is_not_cached(self) -> None:
        # GIVEN an initially empty cluster
        backend = KubernetesStub()
        assert available_chaos_tools(backend) == frozenset()

        # WHEN resources appear and disappear, THEN detection observes each change
        backend.crds.update(CHAOS_MESH_CRDS)
        assert available_chaos_tools(backend) == frozenset({ChaosTool.CHAOS_MESH})
        backend.crds.update(LITMUS_CRDS)
        backend.ready_deployments.add((OPERATOR_NAMESPACE, "litmus"))
        assert available_chaos_tools(backend) == frozenset({ChaosTool.LITMUS, ChaosTool.CHAOS_MESH})
        backend.ready_deployments.clear()
        assert available_chaos_tools(backend) == frozenset({ChaosTool.CHAOS_MESH})


class TestInitialDetection:
    def test_checks_each_cloud_once_and_closes_clients(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN two model namespaces on one cloud and another cloud
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
                [("target", "one"), ("target", "two"), ("neighbor", "three")],
                {"target": Path("target-config"), "neighbor": Path("neighbor-config")},
                logging.getLogger(__name__),
                backend_factory=factory,
            )

        # THEN each unique scope is checked and its client closed
        assert paths == [Path("target-config"), Path("neighbor-config")]
        assert all(backend.api_client.closed for backend in backends)
        assert "target/one: none" in caplog.text
        assert "target/two:" not in caplog.text
        assert "neighbor/three: none" in caplog.text

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


class TestRequireTools:
    def test_machine_model_skips_dependent_test(self) -> None:
        # GIVEN an authoritative machine-cloud result
        backend = JujuBackendStub()
        backend.clients[TARGET.uri] = None

        # WHEN a test requires a tool, THEN only that request is skipped
        with pytest.raises(pytest.skip.Exception, match="Kubernetes model"):
            require_tools_for_model(backend, TARGET)

    def test_absent_tools_skip_dependent_test(self) -> None:
        # GIVEN neither tool in a Kubernetes model
        backend = JujuBackendStub()

        # WHEN a test requires a tool, THEN absence produces a skip
        with pytest.raises(pytest.skip.Exception, match="Neither Litmus nor Chaos Mesh"):
            require_tools_for_model(backend, TARGET)

    @pytest.mark.parametrize("status", [401, 403, 500])
    def test_api_errors_are_not_skipped(self, status: int) -> None:
        # GIVEN available Mesh CRDs but an API failure
        backend = JujuBackendStub()
        backend.kubernetes.crds.update(CHAOS_MESH_CRDS)
        backend.kubernetes.error = ApiException(status=status)

        # WHEN requiring a tool, THEN errors do not cause a skip or fallback
        with pytest.raises(ApiException) as exc_info:
            require_tools_for_model(backend, TARGET)
        assert exc_info.value is backend.kubernetes.error

    def test_missing_model_configuration_is_not_skipped(self) -> None:
        # GIVEN a model with no registered Kubernetes client
        backend = JujuBackendStub()
        del backend.clients[TARGET.uri]

        # WHEN requiring a tool, THEN configuration failure propagates
        with pytest.raises(KeyError):
            require_tools_for_model(backend, TARGET)

    def test_shared_operator_serves_both_models(self) -> None:
        # GIVEN two models sharing a cluster with Litmus and Chaos Mesh
        backend = JujuBackendStub()
        backend.kubernetes.crds.update((*LITMUS_CRDS, *CHAOS_MESH_CRDS))
        backend.kubernetes.ready_deployments.add((OPERATOR_NAMESPACE, "litmus"))
        resolve = unwrap(chaos_tools.chaos_tool_for_model)(backend, None)

        # WHEN both models request a chaos tool
        target_tools = resolve(TARGET)
        neighbor_tools = resolve(NEIGHBOR)

        # THEN both use the shared operator without querying charm configuration
        assert target_tools == neighbor_tools == frozenset({ChaosTool.LITMUS, ChaosTool.CHAOS_MESH})
        assert backend.kubernetes.reads == [("litmus-system", "litmus")] * 2
        assert backend.resolutions == [TARGET.uri, NEIGHBOR.uri]

    def test_selection_is_rechecked_for_each_request(self) -> None:
        # GIVEN a shared operator and an available Chaos Mesh installation
        backend = JujuBackendStub()
        backend.kubernetes.crds.update((*LITMUS_CRDS, *CHAOS_MESH_CRDS))
        backend.kubernetes.ready_deployments.add((OPERATOR_NAMESPACE, "litmus"))
        resolve = unwrap(chaos_tools.chaos_tool_for_model)(backend, None)
        assert resolve(TARGET) == frozenset({ChaosTool.LITMUS, ChaosTool.CHAOS_MESH})

        # WHEN the shared operator stops
        backend.kubernetes.ready_deployments.clear()

        # THEN the next request no longer reports Litmus
        assert resolve(TARGET) == frozenset({ChaosTool.CHAOS_MESH})

    def test_target_fixture_uses_target_model(self) -> None:
        # GIVEN a tool resolver that records the requested model
        models: list[JujuModelHandle] = []

        def resolve(model: JujuModelHandle) -> frozenset[ChaosTool]:
            models.append(model)
            return frozenset({ChaosTool.LITMUS})

        # WHEN the target test requires a chaos tool
        tool = unwrap(chaos_tools.require_chaos_tool)(resolve, TARGET)

        # THEN the target model's tool is returned
        assert tool == frozenset({ChaosTool.LITMUS})
        assert models == [TARGET]


def test_detection_runs_only_when_a_chaos_fixture_is_requested(pytester: pytest.Pytester) -> None:
    # GIVEN both chaos plugins and a backend that records every Kubernetes query
    calls_path = pytester.path / "api_calls.log"
    pytester.makeconftest(
        """
        import logging
        from pathlib import Path

        import pytest
        from juju import JujuModelHandle
        from kubernetes_client import KubernetesBackend, KubernetesClient

        pytest_plugins = ["test_suite.fixtures.chaos_tools", "test_suite.fixtures.chaos_mesh"]
        TARGET = JujuModelHandle(controller="target-controller", model="target-model")
        CALLS = Path(__file__).with_name("api_calls.log")


        def record(operation):
            with CALLS.open("a") as stream:
                stream.write(operation + "\\n")


        def pytest_addoption(parser):
            parser.addoption("--current-state", default="deployed")
            parser.addoption("--target-cloud", default="target")
            parser.addoption("--target-platform", default="kubernetes")
            parser.addoption("--neighbor-cloud", default=None)
            parser.addoption("--neighbor-platform", default=None)


        class ApiClientStub:
            def close(self):
                record("close")


        class BackendStub(KubernetesBackend):
            def __init__(self):
                self.api_client = ApiClientStub()

            def crd_exists(self, name):
                record(name)
                return False

            def deployment_is_ready(self, namespace, name):
                record(namespace + "/" + name)
                return False


        class JujuBackendStub:
            def get_kubernetes_client_for_model(self, model):
                record("model lookup")
                return KubernetesClient(BackendStub())


        @pytest.fixture(scope="session", autouse=True)
        def client_boundary():
            def create_client(kubeconfig):
                record("client creation")
                return BackendStub()

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(KubernetesBackend, "k8s_client", staticmethod(create_client))
                yield


        @pytest.fixture(scope="session")
        def logger():
            return logging.getLogger("chaos-tools-wiring-test")


        @pytest.fixture(scope="session")
        def juju_backend():
            return JujuBackendStub()


        @pytest.fixture(scope="session")
        def cloud_kubeconfigs():
            return {"target": Path("/test/target.yaml")}


        @pytest.fixture(scope="session")
        def target_cloud():
            return "target"


        @pytest.fixture(scope="session")
        def target_platform():
            return "kubernetes"


        @pytest.fixture(scope="session")
        def target_model_ref():
            return TARGET


        @pytest.fixture(scope="session")
        def neighbor_cloud():
            return None


        @pytest.fixture(scope="session")
        def neighbor_model_ref():
            return None


        @pytest.fixture(scope="session")
        def register_preexisting_resources():
            return None
        """
    )
    pytester.makepyfile(
        """
        import pytest


        def test_native_without_chaos_tools():
            pass


        def test_chaos_only(require_chaos_tool):
            pytest.fail("Missing tools must skip before running the test")


        def test_mesh_only(require_chaos_mesh):
            pytest.fail("Missing Chaos Mesh must skip before running the test")
        """
    )

    # WHEN only the unrelated test runs
    unrelated_only = pytester.runpytest("-k", "test_native_without_chaos_tools", "--log-cli-level=INFO")

    # THEN neither plugin queries Kubernetes
    unrelated_only.assert_outcomes(passed=1)
    assert not calls_path.exists()
    assert "Initial chaos tools" not in unrelated_only.stdout.str()
    assert "Chaos Mesh on cloud" not in unrelated_only.stdout.str()

    # WHEN two tool-dependent tests run in the same session
    with_chaos_tests = pytester.runpytest("-k", "test_chaos_only or test_mesh_only", "--log-cli-level=INFO")

    # THEN both skip, with one report per detector and fresh prerequisite checks
    with_chaos_tests.assert_outcomes(skipped=2)
    calls = calls_path.read_text().splitlines()
    assert calls.count("chaosengines.litmuschaos.io") == 2
    assert calls.count("stresschaos.chaos-mesh.org") == 4
    assert calls.count("client creation") == 2
    assert calls.count("close") == 2
    assert with_chaos_tests.stdout.str().count("Initial chaos tools for target-controller:target-model: none.") == 1
    assert with_chaos_tests.stdout.str().count("Chaos Mesh on cloud target: not installed") == 1


class TestPreferredChaosTool:
    @dataclass(frozen=True)
    class Params:
        label: str
        tools: frozenset[ChaosTool]
        expected: ChaosTool | None

    test_cases = [
        Params("both", frozenset({ChaosTool.LITMUS, ChaosTool.CHAOS_MESH}), ChaosTool.LITMUS),
        Params("litmus-only", frozenset({ChaosTool.LITMUS}), ChaosTool.LITMUS),
        Params("mesh-only", frozenset({ChaosTool.CHAOS_MESH}), ChaosTool.CHAOS_MESH),
        Params("neither", frozenset(), None),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=lambda params: params.label)
    def test_prefers_litmus_when_available(self, params: Params) -> None:
        # GIVEN tools that support the same experiment
        tools = params.tools

        # WHEN choosing one tool for that experiment
        tool = preferred_chaos_tool(tools)

        # THEN Litmus is preferred when available
        assert tool == params.expected
