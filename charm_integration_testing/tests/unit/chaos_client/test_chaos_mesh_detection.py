# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from chaos_client.chaos_mesh_detection import CHAOS_MESH_CRDS, chaos_mesh_is_available
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend


class BackendStub(KubernetesBackend):
    def __init__(self) -> None:
        self.crds = set(CHAOS_MESH_CRDS)
        self.crd_errors: dict[str, ApiException] = {}
        self.crd_reads: list[str] = []

    def crd_exists(self, name: str) -> bool:
        self.crd_reads.append(name)
        if name in self.crd_errors:
            raise self.crd_errors[name]
        return name in self.crds


class TestChaosMeshIsAvailable:
    @pytest.mark.parametrize(
        "crds",
        [CHAOS_MESH_CRDS, ("stresschaos.chaos-mesh.org",), ("iochaos.chaos-mesh.org",), ()],
        ids=["both", "stress-only", "io-only", "neither"],
    )
    def test_requires_at_least_one_crd(self, crds: tuple[str, ...]) -> None:
        # GIVEN a cluster with the specified CRDs
        backend = BackendStub()
        backend.crds = set(crds)

        # WHEN checking Chaos Mesh availability
        result = chaos_mesh_is_available(backend)

        # THEN either CRD permits experiments, but both are checked
        assert result is bool(crds)
        assert backend.crd_reads == list(CHAOS_MESH_CRDS)

    @pytest.mark.parametrize("status", [401, 403, 500])
    @pytest.mark.parametrize("crd", CHAOS_MESH_CRDS)
    def test_propagates_api_errors(self, status: int, crd: str) -> None:
        # GIVEN an API failure while reading a required CRD
        backend = BackendStub()
        error = ApiException(status=status)
        backend.crd_errors[crd] = error

        # WHEN checking availability
        with pytest.raises(ApiException) as exc_info:
            chaos_mesh_is_available(backend)

        # THEN the original exception is propagated
        assert exc_info.value is error

    def test_missing_crd_does_not_hide_later_api_error(self) -> None:
        # GIVEN absent StressChaos and an error reading IOChaos
        backend = BackendStub()
        backend.crds.remove("stresschaos.chaos-mesh.org")
        error = ApiException(status=403)
        backend.crd_errors["iochaos.chaos-mesh.org"] = error

        # WHEN checking availability
        with pytest.raises(ApiException) as exc_info:
            chaos_mesh_is_available(backend)

        # THEN the error is not reported as an absent tool
        assert exc_info.value is error
        assert backend.crd_reads == list(CHAOS_MESH_CRDS)

    def test_rechecks_after_installation_and_removal(self) -> None:
        # GIVEN a cluster without Chaos Mesh
        backend = BackendStub()
        backend.crds.clear()
        assert chaos_mesh_is_available(backend) is False

        # WHEN both CRDs are installed, THEN detection observes the change
        backend.crds.update(CHAOS_MESH_CRDS)
        assert chaos_mesh_is_available(backend) is True

        # WHEN IOChaos is removed, THEN stress experiments remain available
        backend.crds.remove("iochaos.chaos-mesh.org")
        assert chaos_mesh_is_available(backend) is True

        # WHEN StressChaos is also removed, THEN no experiments remain available
        backend.crds.clear()
        assert chaos_mesh_is_available(backend) is False
