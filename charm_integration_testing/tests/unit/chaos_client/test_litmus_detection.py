# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass

import pytest
from chaos_client.litmus_detection import litmus_is_available
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend


class BackendStub(KubernetesBackend):
    def __init__(
        self,
        *,
        crd_present: bool = True,
        operator_ready: bool = True,
        crd_error: ApiException | None = None,
        deployment_error: ApiException | None = None,
    ) -> None:
        self.crd_present = crd_present
        self.operator_ready = operator_ready
        self.crd_error = crd_error
        self.deployment_error = deployment_error
        self.crd_reads: list[str] = []
        self.deployment_reads: list[tuple[str, str]] = []

    def crd_exists(self, name: str) -> bool:
        self.crd_reads.append(name)
        if self.crd_error is not None:
            raise self.crd_error
        return self.crd_present

    def deployment_is_ready(self, namespace: str, name: str) -> bool:
        self.deployment_reads.append((namespace, name))
        if self.deployment_error is not None:
            raise self.deployment_error
        return self.operator_ready


class TestLitmusIsAvailable:
    @dataclass(frozen=True)
    class Params:
        label: str
        crd_present: bool
        operator_ready: bool
        expected: bool

    test_cases = [
        Params(label="available", crd_present=True, operator_ready=True, expected=True),
        Params(label="operator-not-ready", crd_present=True, operator_ready=False, expected=False),
        Params(label="crd-absent", crd_present=False, operator_ready=True, expected=False),
        Params(label="both-absent", crd_present=False, operator_ready=False, expected=False),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test_requires_crd_and_ready_operator(self, params: Params) -> None:
        # GIVEN the specified CRD and operator states
        backend = BackendStub(crd_present=params.crd_present, operator_ready=params.operator_ready)

        # WHEN checking Litmus availability
        result = litmus_is_available(backend, "execution-model")

        # THEN availability matches the expected result in the requested namespace
        assert result is params.expected
        assert backend.crd_reads == ["chaosengines.litmuschaos.io"]
        expected_reads = [("execution-model", "chaos-operator-ce")] if params.crd_present else []
        assert backend.deployment_reads == expected_reads

    @pytest.mark.parametrize("status", [401, 403, 500])
    @pytest.mark.parametrize("operation", ["crd", "deployment"])
    def test_propagates_api_errors(self, status: int, operation: str) -> None:
        # GIVEN an API failure at either detection step
        error = ApiException(status=status)
        backend = BackendStub(
            crd_error=error if operation == "crd" else None,
            deployment_error=error if operation == "deployment" else None,
        )

        # WHEN checking Litmus availability
        # THEN the original exception is re-raised
        with pytest.raises(ApiException) as exc_info:
            litmus_is_available(backend, "execution-model")

        assert exc_info.value is error

    def test_rechecks_after_installation_and_removal(self) -> None:
        # GIVEN a cluster without Litmus
        backend = BackendStub(crd_present=False, operator_ready=False)
        assert litmus_is_available(backend, "execution-model") is False

        # WHEN the CRD is installed before the operator becomes ready
        backend.crd_present = True
        assert litmus_is_available(backend, "execution-model") is False
        backend.operator_ready = True

        # THEN the next check detects the ready operator
        assert litmus_is_available(backend, "execution-model") is True

        # WHEN the operator stops
        backend.operator_ready = False

        # THEN Litmus is no longer available
        assert litmus_is_available(backend, "execution-model") is False

        # WHEN the operator is ready but the CRD is removed
        backend.operator_ready = True
        backend.crd_present = False

        # THEN Litmus remains unavailable and each call queried the current state
        assert litmus_is_available(backend, "execution-model") is False
        assert len(backend.crd_reads) == 5
        assert len(backend.deployment_reads) == 3
