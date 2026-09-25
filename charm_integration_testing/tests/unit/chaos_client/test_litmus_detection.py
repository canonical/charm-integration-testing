# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from chaos_client.litmus_detection import LITMUS_CRDS, litmus_is_available
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend


class BackendStub(KubernetesBackend):
    def __init__(self) -> None:
        self.crds = set(LITMUS_CRDS)
        self.ready_deployments = {("litmus-system", "litmus")}
        self.crd_errors: dict[str, ApiException] = {}
        self.deployment_error: ApiException | None = None
        self.crd_reads: list[str] = []
        self.deployment_reads: list[tuple[str, str]] = []

    def crd_exists(self, name: str) -> bool:
        self.crd_reads.append(name)
        if name in self.crd_errors:
            raise self.crd_errors[name]
        return name in self.crds

    def deployment_is_ready(self, namespace: str, name: str) -> bool:
        self.deployment_reads.append((namespace, name))
        if self.deployment_error is not None:
            raise self.deployment_error
        return (namespace, name) in self.ready_deployments


class TestLitmusIsAvailable:
    @pytest.mark.parametrize("missing_crd", [None, *LITMUS_CRDS])
    @pytest.mark.parametrize("operator_ready", [False, True])
    def test_requires_all_crds_and_ready_operator(self, missing_crd: str | None, operator_ready: bool) -> None:
        # GIVEN the required CRDs and shared operator states
        backend = BackendStub()
        if missing_crd is not None:
            backend.crds.remove(missing_crd)
        if not operator_ready:
            backend.ready_deployments.clear()

        # WHEN checking Litmus availability
        result = litmus_is_available(backend)

        # THEN all CRDs and the shared operator are required
        assert result is (missing_crd is None and operator_ready)
        assert backend.crd_reads == list(LITMUS_CRDS)
        expected_reads = [("litmus-system", "litmus")] if missing_crd is None else []
        assert backend.deployment_reads == expected_reads

    @pytest.mark.parametrize("status", [401, 403, 500])
    @pytest.mark.parametrize("operation", [*LITMUS_CRDS, "deployment"])
    def test_propagates_api_errors(self, status: int, operation: str) -> None:
        # GIVEN an API failure while reading a required resource
        backend = BackendStub()
        error = ApiException(status=status)
        if operation == "deployment":
            backend.deployment_error = error
        else:
            backend.crd_errors[operation] = error

        # WHEN checking Litmus availability
        with pytest.raises(ApiException) as exc_info:
            litmus_is_available(backend)

        # THEN the original exception is propagated
        assert exc_info.value is error

    def test_missing_crd_does_not_hide_later_api_error(self) -> None:
        # GIVEN an absent ChaosEngine CRD and an error reading ChaosResults
        backend = BackendStub()
        backend.crds.remove("chaosengines.litmuschaos.io")
        error = ApiException(status=403)
        backend.crd_errors["chaosresults.litmuschaos.io"] = error

        # WHEN checking Litmus availability
        with pytest.raises(ApiException) as exc_info:
            litmus_is_available(backend)

        # THEN the error is not reported as an absent tool
        assert exc_info.value is error

    @pytest.mark.parametrize("deployment", [("test-model", "litmus"), ("litmus-system", "chaos-operator-ce")])
    def test_other_operators_do_not_satisfy_detection(self, deployment: tuple[str, str]) -> None:
        # GIVEN a ready operator with a different namespace or name
        backend = BackendStub()
        backend.ready_deployments = {deployment}

        # WHEN checking Litmus availability
        result = litmus_is_available(backend)

        # THEN only the shared Helm operator is accepted
        assert result is False
        assert backend.deployment_reads == [("litmus-system", "litmus")]

    def test_rechecks_after_installation_and_removal(self) -> None:
        # GIVEN a cluster without Litmus
        backend = BackendStub()
        backend.crds.clear()
        backend.ready_deployments.clear()
        assert litmus_is_available(backend) is False

        # WHEN the CRDs and operator become available
        backend.crds.update(LITMUS_CRDS)
        assert litmus_is_available(backend) is False
        backend.ready_deployments.add(("litmus-system", "litmus"))

        # THEN the next check detects the operator
        assert litmus_is_available(backend) is True

        # WHEN a required CRD is removed
        backend.crds.remove("chaosresults.litmuschaos.io")

        # THEN the next check reports Litmus as unavailable
        assert litmus_is_available(backend) is False
