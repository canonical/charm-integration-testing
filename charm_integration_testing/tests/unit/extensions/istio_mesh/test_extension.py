# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from pathlib import Path
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

    @pytest.mark.parametrize("dependent", [ISTIO_BEACON_CHARM, ISTIO_INGRESS_CHARM])
    @pytest.mark.parametrize("existing", [False, True])
    def test_prepares_control_plane_before_dependents(
        self, tmp_path: Path, logger: logging.Logger, dependent: str, existing: bool
    ) -> None:
        backend = JujuStub(applications={"mesh": "istio-k8s"} if existing else {})
        store = CharmhubStub()
        bundle = tmp_path / "bundle.yaml"
        bundle.write_text(
            f"applications:\n  dependent:\n    charm: {dependent}\n---\napplications:\n  dependent:\n    offers: {{}}\n"
        )
        extension = IstioMeshExtension(backend, logger, cast(CharmhubClient, store))
        extension.pre_deploy(TEST_MODEL, [bundle])
        # Simulate Juju reporting the application on a subsequent deployment.
        if not existing:
            backend.applications["istio-k8s"] = "istio-k8s"
        extension.pre_deploy(TEST_MODEL, [bundle])
        assert store.requested == ([] if existing else ["istio-k8s"])
        assert len(backend.deployed) == (0 if existing else 1)
        assert backend.waited_idle == []
        assert backend.waited_settled == []

    @pytest.mark.parametrize(
        "content,is_k8s",
        [
            ("applications: {other: {charm: postgresql}}", True),
            ("applications: {beacon: {charm: istio-beacon-k8s}}", False),
            ("applications: {beacon: {charm: istio-beacon-k8s}, mesh: {charm: istio-k8s, revision: 82}}", True),
        ],
    )
    def test_pre_deploy_leaves_unrelated_and_explicit_bundles_unchanged(
        self, tmp_path: Path, logger: logging.Logger, content: str, is_k8s: bool
    ) -> None:
        backend = JujuStub()
        backend.is_k8s = is_k8s
        bundle = tmp_path / "bundle.yaml"
        bundle.write_text(content)
        store = CharmhubStub()
        IstioMeshExtension(backend, logger, cast(CharmhubClient, store)).pre_deploy(TEST_MODEL, [bundle])
        assert backend.deployed == []
        assert store.requested == []

    @pytest.mark.parametrize("applications", ["[istio-beacon-k8s]", "istio-beacon-k8s", "null", "42"])
    def test_pre_deploy_skips_non_mapping_applications(
        self, tmp_path: Path, logger: logging.Logger, applications: str
    ) -> None:
        # GIVEN a bundle whose applications section is not a mapping
        bundle = tmp_path / "bundle.yaml"
        bundle.write_text(f"applications: {applications}\n", encoding="utf-8")
        backend = JujuStub()
        store = CharmhubStub()
        extension = IstioMeshExtension(backend, logger, cast(CharmhubClient, store))

        # WHEN inspecting the bundle before deployment
        extension.pre_deploy(TEST_MODEL, [bundle])

        # THEN skip prerequisite deployment without querying Charmhub
        assert backend.deployed == []
        assert store.requested == []

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
