# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend
from test_suite.fixtures import chaos_mesh

pytest_plugins = ["pytester"]

TARGET_CONFIG = Path("/test/target.yaml")
NEIGHBOR_CONFIG = Path("/test/neighbor.yaml")
LOGGER = logging.getLogger(__name__)


class ApiClientStub:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class CrdApiStub:
    def __init__(self, error: ApiException | None = None) -> None:
        self.error = error
        self.names: list[str] = []

    def read_custom_resource_definition(self, name: str) -> None:
        self.names.append(name)
        if self.error is not None:
            raise self.error


class BackendStub(KubernetesBackend):
    def __init__(self, error: ApiException | None = None) -> None:
        self.api_client = ApiClientStub()
        self.apiextensions_v1_api = CrdApiStub(error)


class BackendFactoryStub:
    def __init__(self) -> None:
        self.backends: dict[Path, BackendStub] = {}
        self.calls: list[Path] = []

    def __call__(self, *, kubeconfig: Path) -> KubernetesBackend:
        self.calls.append(kubeconfig)
        return self.backends[kubeconfig]


@pytest.fixture
def backend_factory(monkeypatch: pytest.MonkeyPatch) -> BackendFactoryStub:
    factory = BackendFactoryStub()
    # Replace only the client creation boundary; retain the real crd_exists implementation.
    monkeypatch.setattr(KubernetesBackend, "k8s_client", factory)
    return factory


class ConfigStub:
    def __init__(self, options: dict[str, str | None]) -> None:
        self.options = options

    def getoption(self, name: str) -> str | None:
        return self.options.get(name)


class RequestStub:
    def __init__(self, options: dict[str, str | None]) -> None:
        self.config = ConfigStub(options)


def run_detection(
    kubeconfigs: dict[str, Path],
    *,
    target_platform: str = "kubernetes",
    neighbor_cloud: str | None = None,
    neighbor_platform: str | None = None,
) -> None:
    request = RequestStub(
        {
            "--target-cloud": "target",
            "--target-platform": target_platform,
            "--neighbor-cloud": neighbor_cloud,
            "--neighbor-platform": neighbor_platform,
        }
    )
    detect = cast(
        Callable[[pytest.FixtureRequest, dict[str, Path], logging.Logger], None],
        inspect.unwrap(chaos_mesh.detect_chaos_mesh),
    )
    detect(cast(pytest.FixtureRequest, request), kubeconfigs, LOGGER)


def run_gate(kubeconfigs: dict[str, Path], platform: str = "kubernetes") -> None:
    gate = cast(
        Callable[[str, str, dict[str, Path]], None],
        inspect.unwrap(chaos_mesh.require_chaos_mesh),
    )
    gate(platform, "target", kubeconfigs)


def test_detection_checks_only_stresschaos_and_closes_client(backend_factory: BackendFactoryStub) -> None:
    # GIVEN a backend whose CRD reads succeed
    backend = BackendStub()
    backend_factory.backends[TARGET_CONFIG] = backend

    # WHEN checking whether Chaos Mesh is installed
    installed = chaos_mesh._chaos_mesh_installed(TARGET_CONFIG)

    # THEN IOChaos is not part of the installation check and the client is closed
    assert installed is True
    assert backend.apiextensions_v1_api.names == ["stresschaos.chaos-mesh.org"]
    assert backend.api_client.close_calls == 1


def test_missing_crd_returns_false_and_closes_client(backend_factory: BackendFactoryStub) -> None:
    # GIVEN the Kubernetes API reports the CRD as absent
    backend = BackendStub(ApiException(status=404))
    backend_factory.backends[TARGET_CONFIG] = backend

    # WHEN checking installation
    installed = chaos_mesh._chaos_mesh_installed(TARGET_CONFIG)

    # THEN the absence is a negative result, not an API error
    assert installed is False
    assert backend.api_client.close_calls == 1


@dataclass(frozen=True)
class ApiErrorParams:
    name: str
    status: int


@pytest.mark.parametrize(
    "params",
    [ApiErrorParams("forbidden", 403), ApiErrorParams("server-error", 500)],
    ids=lambda params: params.name,
)
def test_api_errors_propagate_from_detection_and_gate(
    backend_factory: BackendFactoryStub, params: ApiErrorParams
) -> None:
    # GIVEN an API failure rather than a missing CRD
    error = ApiException(status=params.status)
    backend = BackendStub(error)
    backend_factory.backends[TARGET_CONFIG] = backend
    kubeconfigs = {"target": TARGET_CONFIG}

    # WHEN detecting at session setup or checking the target prerequisite
    with pytest.raises(ApiException) as detection_error:
        run_detection(kubeconfigs)
    with pytest.raises(ApiException) as gate_error:
        run_gate(kubeconfigs)

    # THEN neither path converts the error to absence or skip, and both close their clients
    assert detection_error.value is error
    assert gate_error.value is error
    assert backend.api_client.close_calls == 2


def test_detection_keeps_cloud_results_separate(
    backend_factory: BackendFactoryStub, caplog: pytest.LogCaptureFixture
) -> None:
    # GIVEN two participating clouds and an unrelated configured cloud
    backend_factory.backends[TARGET_CONFIG] = BackendStub()
    backend_factory.backends[NEIGHBOR_CONFIG] = BackendStub(ApiException(status=404))
    kubeconfigs = {"target": TARGET_CONFIG, "neighbor": NEIGHBOR_CONFIG, "unused": Path("/test/unused.yaml")}

    # WHEN the neighbor inherits the target's Kubernetes platform
    with caplog.at_level(logging.INFO, logger=LOGGER.name):
        run_detection(kubeconfigs, neighbor_cloud="neighbor")

    # THEN each participating cloud is checked and logged independently
    assert backend_factory.calls == [TARGET_CONFIG, NEIGHBOR_CONFIG]
    assert "Chaos Mesh on cloud target: detected" in caplog.messages
    assert "Chaos Mesh on cloud neighbor: not installed" in caplog.messages


def test_detection_checks_shared_cloud_once(backend_factory: BackendFactoryStub) -> None:
    # GIVEN target and neighbor share one Kubernetes cloud
    backend_factory.backends[TARGET_CONFIG] = BackendStub()

    # WHEN detecting session prerequisites
    run_detection({"target": TARGET_CONFIG}, neighbor_cloud="target")

    # THEN there is only one API client creation
    assert backend_factory.calls == [TARGET_CONFIG]


def test_machine_target_does_not_hide_kubernetes_neighbor(backend_factory: BackendFactoryStub) -> None:
    # GIVEN a mixed-platform CMR execution
    backend_factory.backends[NEIGHBOR_CONFIG] = BackendStub()

    # WHEN detecting availability
    run_detection(
        {"neighbor": NEIGHBOR_CONFIG},
        target_platform="machine",
        neighbor_cloud="neighbor",
        neighbor_platform="kubernetes",
    )

    # THEN only the Kubernetes neighbor is queried
    assert backend_factory.calls == [NEIGHBOR_CONFIG]


def test_machine_gate_skips_without_api_access(backend_factory: BackendFactoryStub) -> None:
    # GIVEN a non-Kubernetes target
    run_detection({}, target_platform="machine")

    # WHEN a Chaos Mesh-only test requests its prerequisite
    with pytest.raises(pytest.skip.Exception, match="require a Kubernetes target"):
        run_gate({}, platform="machine")

    # THEN neither session detection nor the gate accesses Kubernetes
    assert backend_factory.calls == []


def test_missing_kubeconfig_is_not_reported_as_missing_chaos_mesh(
    backend_factory: BackendFactoryStub, caplog: pytest.LogCaptureFixture
) -> None:
    # GIVEN a Kubernetes target with no configured kubeconfig
    # WHEN session detection runs and a Chaos Mesh test requests its prerequisite
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        run_detection({})
    with pytest.raises(pytest.fail.Exception, match="no kubeconfig configured"):
        run_gate({})

    # THEN configuration is reported as unavailable, not as a missing CRD
    assert "Chaos Mesh detection unavailable for cloud target: no kubeconfig configured." in caplog.messages
    assert backend_factory.calls == []


def test_gate_uses_target_not_neighbor_availability(backend_factory: BackendFactoryStub) -> None:
    # GIVEN an absent target CRD and an installed neighbor
    backend_factory.backends[TARGET_CONFIG] = BackendStub(ApiException(status=404))
    backend_factory.backends[NEIGHBOR_CONFIG] = BackendStub()

    # WHEN checking the target's prerequisite
    with pytest.raises(pytest.skip.Exception, match="cloud 'target'.*stresschaos"):
        run_gate({"target": TARGET_CONFIG, "neighbor": NEIGHBOR_CONFIG})

    # THEN the neighbor does not satisfy the target's prerequisite
    assert backend_factory.calls == [TARGET_CONFIG]


def test_gate_rechecks_after_session_detection(backend_factory: BackendFactoryStub) -> None:
    # GIVEN a session that initially observed no Chaos Mesh installation
    backend_factory.backends[TARGET_CONFIG] = BackendStub(ApiException(status=404))
    kubeconfigs = {"target": TARGET_CONFIG}
    run_detection(kubeconfigs)

    # WHEN Chaos Mesh becomes available before the target test
    backend_factory.backends[TARGET_CONFIG] = BackendStub()
    run_gate(kubeconfigs)

    # THEN the gate succeeds using a fresh lookup instead of the initial absence
    assert backend_factory.calls == [TARGET_CONFIG, TARGET_CONFIG]


def test_pytest_skip_preserves_other_tests_and_scheduler(pytester: pytest.Pytester) -> None:
    # GIVEN a miniature suite with the real fixtures and scheduler, but no cluster access
    pytester.makeconftest(
        """
        import logging
        from pathlib import Path

        import pytest
        from test_suite.fixtures import chaos_mesh

        pytest_plugins = ["test_suite.fixtures.chaos_mesh", "test_suite.scheduler.plugin"]

        def pytest_addoption(parser):
            parser.addoption("--target-cloud", default="target")
            parser.addoption("--target-platform", default="kubernetes")
            parser.addoption("--neighbor-cloud", default=None)
            parser.addoption("--neighbor-platform", default=None)

        @pytest.fixture(scope="session")
        def logger():
            return logging.getLogger("chaos-mesh-test")

        @pytest.fixture(scope="session")
        def cloud_kubeconfigs():
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(chaos_mesh, "_chaos_mesh_installed", lambda kubeconfig: False)
                yield {"target": Path("/test/target.yaml")}

        @pytest.fixture
        def target_platform():
            return "kubernetes"

        @pytest.fixture(scope="session")
        def target_cloud():
            return "target"
        """
    )
    pytester.makepyfile(
        """
        import pytest
        from test_suite.scheduler.states import State

        @pytest.mark.state(requires=State.DEPLOYED)
        def test_chaos_mesh_only(require_chaos_mesh):
            pytest.fail("A missing Chaos Mesh installation must skip before the body runs")

        @pytest.mark.state(requires=State.DEPLOYED)
        def test_next_state_test():
            pass

        def test_native_without_chaos_mesh():
            pass
        """
    )

    # WHEN executing from an already deployed state, so no deployment bridges are needed
    result = pytester.runpytest("--current-state=deployed", "-v", "-rs")

    # THEN only the Chaos Mesh-only test skips, without failing or halting the scheduler
    result.assert_outcomes(passed=2, skipped=1)
    result.stdout.fnmatch_lines(["*test_chaos_mesh_only SKIPPED*", "*test_next_state_test PASSED*"])
    result.stdout.fnmatch_lines(["*CRD 'stresschaos.chaos-mesh.org' is absent*"])
