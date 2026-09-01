# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from extensions.istio_beacon_mesh.extension import (
    GATEWAY_CRD_NAME,
    ISTIO_BEACON_CHARM,
    ISTIO_CONTROL_PLANE_CHANNEL,
    ISTIO_CONTROL_PLANE_CHARM,
    IstioBeaconMeshExtension,
)
from juju import JujuModelHandle
from kubernetes_client import KubernetesClient

from ..shared import JujuStub as JujuStubBase

TEST_MODEL: JujuModelHandle = JujuModelHandle(controller="test-controller", model="test-model")


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


class TestIstioBeaconMeshExtension:
    @pytest.fixture
    def logger(self) -> logging.Logger:
        return logging.getLogger("test")

    @pytest.fixture
    def extension(self, juju: JujuStub, logger: logging.Logger) -> IstioBeaconMeshExtension:
        return IstioBeaconMeshExtension(juju, logger)

    class TestPostDeploy:
        def test_ignores_models_without_istio_beacon(self, logger: logging.Logger) -> None:
            # GIVEN a model with no istio-beacon-k8s application
            juju = JujuStub(applications={"other-app": "other-charm"})
            extension = IstioBeaconMeshExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN nothing is deployed and the cluster is never queried
            assert juju.deployed == []

        def test_skips_deploy_when_gateway_crd_already_present(self, logger: logging.Logger) -> None:
            # GIVEN istio-beacon-k8s is present and the Gateway API CRD already exists
            juju = JujuStub(
                applications={"istio-beacon-k8s": ISTIO_BEACON_CHARM},
                kubernetes_client=KubernetesClientStub({GATEWAY_CRD_NAME}),
            )
            extension = IstioBeaconMeshExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN istio-k8s is not deployed
            assert juju.deployed == []

        def test_deploys_istio_when_beacon_present_and_crd_missing(self, logger: logging.Logger) -> None:
            # GIVEN istio-beacon-k8s is present and the Gateway API CRD is missing
            juju = JujuStub(
                applications={"istio-beacon-k8s": ISTIO_BEACON_CHARM},
                kubernetes_client=KubernetesClientStub(set()),
            )
            extension = IstioBeaconMeshExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN istio-k8s is deployed on its pinned channel and awaited to settle
            assert juju.deployed == [(TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, ISTIO_CONTROL_PLANE_CHARM)]
            assert juju.deployed_with_channel == [
                (TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, ISTIO_CONTROL_PLANE_CHARM, ISTIO_CONTROL_PLANE_CHANNEL)
            ]
            assert juju.waited_settled == [(TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, "0:15:00")]

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
            extension = IstioBeaconMeshExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN no new deploy happens; the extension waits on the existing application
            assert juju.deployed == []
            assert juju.waited_settled == [(TEST_MODEL.uri, "my-istio", "0:15:00")]

        def test_ignores_models_on_non_k8s_controllers(self, logger: logging.Logger) -> None:
            # GIVEN istio-beacon-k8s is present but the controller isn't backed by Kubernetes
            juju = JujuStub(applications={"istio-beacon-k8s": ISTIO_BEACON_CHARM}, kubernetes_client=None)
            extension = IstioBeaconMeshExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN nothing is deployed
            assert juju.deployed == []
