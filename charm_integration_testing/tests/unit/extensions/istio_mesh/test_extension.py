# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from extensions.istio_mesh.extension import (
    GATEWAY_CRD_NAME,
    ISTIO_CONTROL_PLANE_CHANNEL,
    ISTIO_CONTROL_PLANE_CHARM,
    ISTIO_MESH_DEPENDENT_CHARMS,
    IstioMeshExtension,
)
from juju import JujuModelHandle
from kubernetes_client import KubernetesClient

from ..shared import JujuStub as JujuStubBase

TEST_MODEL: JujuModelHandle = JujuModelHandle(controller="test-controller", model="test-model")
ISTIO_BEACON_CHARM = "istio-beacon-k8s"
ISTIO_INGRESS_CHARM = "istio-ingress-k8s"


class KubernetesClientStub(KubernetesClient):
    """Fake KubernetesClient that answers crd_exists() from a preset set, without a real cluster."""

    def __init__(self, present_crds: set[str]) -> None:
        self._present_crds = present_crds

    def crd_exists(self, name: str) -> bool:
        return name in self._present_crds


@dataclass
class JujuStub(JujuStubBase):
    kubernetes_client: KubernetesClient | None = None
    deployed_with_channel: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)

    def get_kubernetes_client_for_controller(self, controller: str) -> KubernetesClient | None:
        return self.kubernetes_client

    def deploy_application(
        self,
        model: JujuModelHandle,
        charm: str,
        application: str | None = None,
        config: dict[str, Any] | None = None,
        trust: bool = False,
        force: bool = False,
        channel: str | None = None,
    ) -> None:
        self.deployed_with_channel.append((model.uri, charm, application, channel))
        super().deploy_application(model, charm, application=application, config=config, trust=trust, force=force)


class TestIstioMeshExtension:
    @pytest.fixture
    def logger(self) -> logging.Logger:
        return logging.getLogger("test")

    def test_dependent_charms_are_istio_beacon_and_istio_ingress(self) -> None:
        # Documents the exact set covered by this extension, so an addition/removal here
        # is a visible, deliberate test change rather than a silent behavior change.
        assert ISTIO_MESH_DEPENDENT_CHARMS == frozenset({ISTIO_BEACON_CHARM, ISTIO_INGRESS_CHARM})

    class TestPostDeploy:
        @pytest.mark.parametrize("dependent_charm", [ISTIO_BEACON_CHARM, ISTIO_INGRESS_CHARM])
        def test_ignores_models_without_a_dependent_charm(self, logger: logging.Logger, dependent_charm: str) -> None:
            # GIVEN a model with neither istio-beacon-k8s nor istio-ingress-k8s
            juju = JujuStub(applications={"other-app": "other-charm"})
            extension = IstioMeshExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN nothing is deployed and the cluster is never queried
            assert juju.deployed == []

        @pytest.mark.parametrize("dependent_charm", [ISTIO_BEACON_CHARM, ISTIO_INGRESS_CHARM])
        def test_skips_deploy_when_gateway_crd_already_present(
            self, logger: logging.Logger, dependent_charm: str
        ) -> None:
            # GIVEN a dependent charm is present and the Gateway API CRD already exists
            juju = JujuStub(
                applications={"dependent": dependent_charm},
                kubernetes_client=KubernetesClientStub({GATEWAY_CRD_NAME}),
            )
            extension = IstioMeshExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN istio-k8s is not deployed
            assert juju.deployed == []

        @pytest.mark.parametrize("dependent_charm", [ISTIO_BEACON_CHARM, ISTIO_INGRESS_CHARM])
        def test_deploys_istio_when_dependent_present_and_crd_missing(
            self, logger: logging.Logger, dependent_charm: str
        ) -> None:
            # GIVEN a dependent charm is present and the Gateway API CRD is missing
            juju = JujuStub(
                applications={"dependent": dependent_charm},
                kubernetes_client=KubernetesClientStub(set()),
            )
            extension = IstioMeshExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN istio-k8s is deployed on its pinned channel and awaited to settle
            assert juju.deployed == [(TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, ISTIO_CONTROL_PLANE_CHARM)]
            assert juju.deployed_with_channel == [
                (TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, ISTIO_CONTROL_PLANE_CHARM, ISTIO_CONTROL_PLANE_CHANNEL)
            ]
            assert juju.waited_settled == [(TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, "0:15:00")]

        def test_deploys_istio_only_once_when_both_dependents_present(self, logger: logging.Logger) -> None:
            # GIVEN both istio-beacon-k8s and istio-ingress-k8s are present and the CRD is missing
            juju = JujuStub(
                applications={"beacon": ISTIO_BEACON_CHARM, "ingress": ISTIO_INGRESS_CHARM},
                kubernetes_client=KubernetesClientStub(set()),
            )
            extension = IstioMeshExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN istio-k8s is deployed exactly once
            assert juju.deployed == [(TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, ISTIO_CONTROL_PLANE_CHARM)]

        def test_skips_deploy_when_istio_already_deployed_under_a_different_application_name(
            self, logger: logging.Logger
        ) -> None:
            # GIVEN istio-beacon-k8s is present, the CRD is missing, but istio-k8s is already
            # deployed under a custom application name
            juju = JujuStub(
                applications={
                    "istio-beacon-k8s": ISTIO_BEACON_CHARM,
                    "my-istio": ISTIO_CONTROL_PLANE_CHARM,
                },
                kubernetes_client=KubernetesClientStub(set()),
            )
            extension = IstioMeshExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN no new deploy happens; the extension waits on the existing application
            assert juju.deployed == []
            assert juju.waited_settled == [(TEST_MODEL.uri, "my-istio", "0:15:00")]

        def test_ignores_models_on_non_k8s_controllers(self, logger: logging.Logger) -> None:
            # GIVEN istio-beacon-k8s is present but the controller isn't backed by Kubernetes
            juju = JujuStub(applications={"istio-beacon-k8s": ISTIO_BEACON_CHARM}, kubernetes_client=None)
            extension = IstioMeshExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN nothing is deployed
            assert juju.deployed == []
