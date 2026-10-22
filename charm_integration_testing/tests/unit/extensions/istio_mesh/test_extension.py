# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from extensions.istio_mesh.extension import (
    ISTIO_CONTROL_PLANE_CHARM,
    ISTIO_MESH_DEPENDENT_CHARMS,
    IstioMeshExtension,
)
from juju import JujuModelHandle, JujuVersion

from bundle_builder_x import Charm, CharmChannel, CharmhubClient

from ..shared import JujuStub as JujuStubBase

TEST_MODEL: JujuModelHandle = JujuModelHandle(controller="test-controller", model="test-model")
ISTIO_BEACON_CHARM = "istio-beacon-k8s"
ISTIO_INGRESS_CHARM = "istio-ingress-k8s"


@dataclass
class JujuStub(JujuStubBase):
    deployed_with_channel: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)
    deployed_releases: list[tuple[int | None, str | None]] = field(default_factory=list)

    def version(self, model: JujuModelHandle) -> JujuVersion:
        return JujuVersion.parse("4.0.15")

    def model_architecture(self, model: JujuModelHandle) -> str:
        return "amd64"

    def deploy_application(
        self,
        model: JujuModelHandle,
        charm: str,
        application: str | None = None,
        config: dict[str, Any] | None = None,
        trust: bool = False,
        force: bool = False,
        channel: str | None = None,
        *,
        revision: int | None = None,
        base: str | None = None,
    ) -> None:
        self.deployed_with_channel.append((model.uri, charm, application, channel))
        self.deployed_releases.append((revision, base))
        super().deploy_application(
            model,
            charm,
            application=application,
            config=config,
            trust=trust,
            force=force,
            channel=channel,
            revision=revision,
            base=base,
        )


class CharmhubStub:
    def __init__(self, channel: str = "test-track/candidate") -> None:
        self.channel = channel
        self.requested: list[str] = []

    def charm_from_store(self, **kwargs: Any) -> Charm:
        self.requested.append(kwargs["charm_name"])
        return Charm(
            name=kwargs["charm_name"],
            channel=CharmChannel.model_validate(self.channel),
            revision=1,
            ubuntu_version="24.04",
            ubuntu_arch=kwargs["ubuntu_arch"],
            endpoints={},
            platforms=["kubernetes"],
        )


class TestIstioMeshExtension:
    @pytest.fixture
    def logger(self) -> logging.Logger:
        return logging.getLogger("test")

    def test_dependent_charms_are_istio_beacon_and_istio_ingress(self) -> None:
        # Documents the exact set covered by this extension, so an addition/removal here
        # is a visible, deliberate test change rather than a silent behavior change.
        assert ISTIO_MESH_DEPENDENT_CHARMS == frozenset({ISTIO_BEACON_CHARM, ISTIO_INGRESS_CHARM})

    class TestPostDeploy:
        def test_ignores_models_without_a_dependent_charm(self, logger: logging.Logger) -> None:
            # GIVEN a model with neither istio-beacon-k8s nor istio-ingress-k8s
            juju = JujuStub(applications={"other-app": "other-charm"})
            charmhub = CharmhubStub()
            extension = IstioMeshExtension(juju, logger, cast(CharmhubClient, charmhub))

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN nothing is deployed
            assert juju.deployed == []
            assert charmhub.requested == []

        @pytest.mark.parametrize("dependent_charm", [ISTIO_BEACON_CHARM, ISTIO_INGRESS_CHARM])
        def test_deploys_istio_when_dependent_present_and_istio_not_yet_in_model(
            self, logger: logging.Logger, dependent_charm: str
        ) -> None:
            # GIVEN a dependent charm is present with no istio-k8s of its own in the model
            juju = JujuStub(
                applications={"dependent": dependent_charm},
            )
            charmhub = CharmhubStub()
            extension = IstioMeshExtension(juju, logger, cast(CharmhubClient, charmhub))

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN istio-k8s is deployed on the resolved default channel and awaited to settle
            assert juju.deployed == [(TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, ISTIO_CONTROL_PLANE_CHARM)]
            assert juju.deployed_with_channel == [
                (TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, ISTIO_CONTROL_PLANE_CHARM, charmhub.channel)
            ]
            assert charmhub.requested == [ISTIO_CONTROL_PLANE_CHARM]
            assert juju.deployed_releases == [(1, "ubuntu@24.04")]
            assert juju.waited_settled == [(TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, "0:15:00")]

        @pytest.mark.parametrize("dependent_charm", [ISTIO_BEACON_CHARM, ISTIO_INGRESS_CHARM])
        def test_skips_deploy_when_istio_already_deployed_in_the_same_model(
            self, logger: logging.Logger, dependent_charm: str
        ) -> None:
            # GIVEN the dependent charm and istio-k8s are already both in the model
            juju = JujuStub(
                applications={"dependent": dependent_charm, ISTIO_CONTROL_PLANE_CHARM: ISTIO_CONTROL_PLANE_CHARM},
            )
            charmhub = CharmhubStub()
            extension = IstioMeshExtension(juju, logger, cast(CharmhubClient, charmhub))

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN no new deploy happens, but the extension still waits for it to settle
            assert juju.deployed == []
            assert charmhub.requested == []
            assert juju.waited_settled == [(TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, "0:15:00")]

        def test_deploys_istio_only_once_when_both_dependents_present(self, logger: logging.Logger) -> None:
            # GIVEN both istio-beacon-k8s and istio-ingress-k8s are present with no istio-k8s yet
            juju = JujuStub(
                applications={"beacon": ISTIO_BEACON_CHARM, "ingress": ISTIO_INGRESS_CHARM},
            )
            charmhub = CharmhubStub()
            extension = IstioMeshExtension(juju, logger, cast(CharmhubClient, charmhub))

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN istio-k8s is deployed exactly once
            assert juju.deployed == [(TEST_MODEL.uri, ISTIO_CONTROL_PLANE_CHARM, ISTIO_CONTROL_PLANE_CHARM)]
            assert charmhub.requested == [ISTIO_CONTROL_PLANE_CHARM]

        def test_skips_deploy_when_istio_already_deployed_under_a_different_application_name(
            self, logger: logging.Logger
        ) -> None:
            # GIVEN istio-beacon-k8s is present and istio-k8s is already deployed under a
            # custom application name
            juju = JujuStub(
                applications={
                    "istio-beacon-k8s": ISTIO_BEACON_CHARM,
                    "my-istio": ISTIO_CONTROL_PLANE_CHARM,
                },
            )
            charmhub = CharmhubStub()
            extension = IstioMeshExtension(juju, logger, cast(CharmhubClient, charmhub))

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN no new deploy happens; the extension waits on the existing application
            assert juju.deployed == []
            assert charmhub.requested == []
            assert juju.waited_settled == [(TEST_MODEL.uri, "my-istio", "0:15:00")]

        def test_ignores_models_on_non_k8s_controllers(self, logger: logging.Logger) -> None:
            # GIVEN istio-beacon-k8s is present but the controller isn't backed by Kubernetes
            juju = JujuStub(applications={"istio-beacon-k8s": ISTIO_BEACON_CHARM}, is_k8s=False)
            charmhub = CharmhubStub()
            extension = IstioMeshExtension(juju, logger, cast(CharmhubClient, charmhub))

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN nothing is deployed
            assert juju.deployed == []
            assert charmhub.requested == []
