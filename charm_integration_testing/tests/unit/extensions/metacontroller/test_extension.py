# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from extensions.metacontroller.extension import (
    METACONTROLLER_CHANNEL,
    METACONTROLLER_CHARM,
    METACONTROLLER_DEPENDENT_CHARMS,
    MetacontrollerExtension,
)
from juju import JujuModelHandle

from ..shared import JujuStub as JujuStubBase

TEST_MODEL: JujuModelHandle = JujuModelHandle(controller="test-controller", model="test-model")
KFP_PROFILE_CONTROLLER_CHARM = "kfp-profile-controller"


@dataclass
class JujuStub(JujuStubBase):
    deployed_with_channel: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)

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


class TestMetacontrollerExtension:
    @pytest.fixture
    def logger(self) -> logging.Logger:
        return logging.getLogger("test")

    def test_dependent_charms_are_kfp_profile_controller(self) -> None:
        # Documents the exact set covered by this extension, so an addition/removal here
        # is a visible, deliberate test change rather than a silent behavior change.
        assert METACONTROLLER_DEPENDENT_CHARMS == frozenset({KFP_PROFILE_CONTROLLER_CHARM})

    class TestPostDeploy:
        def test_ignores_models_without_a_dependent_charm(self, logger: logging.Logger) -> None:
            # GIVEN a model with no kfp-profile-controller
            juju = JujuStub(applications={"other-app": "other-charm"})
            extension = MetacontrollerExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN nothing is deployed
            assert juju.deployed == []

        def test_deploys_metacontroller_when_dependent_present_and_not_yet_in_model(
            self, logger: logging.Logger
        ) -> None:
            # GIVEN kfp-profile-controller is present with no metacontroller-operator of its own
            juju = JujuStub(
                applications={"dependent": KFP_PROFILE_CONTROLLER_CHARM},
            )
            extension = MetacontrollerExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN metacontroller-operator is deployed on its pinned channel and awaited to settle
            assert juju.deployed == [(TEST_MODEL.uri, METACONTROLLER_CHARM, METACONTROLLER_CHARM)]
            assert juju.deployed_with_channel == [
                (TEST_MODEL.uri, METACONTROLLER_CHARM, METACONTROLLER_CHARM, METACONTROLLER_CHANNEL)
            ]
            assert juju.waited_settled == [(TEST_MODEL.uri, METACONTROLLER_CHARM, "0:15:00")]

        def test_skips_deploy_when_metacontroller_already_deployed_in_the_same_model(
            self, logger: logging.Logger
        ) -> None:
            # GIVEN the dependent charm and metacontroller-operator are already both in the model
            juju = JujuStub(
                applications={
                    "dependent": KFP_PROFILE_CONTROLLER_CHARM,
                    METACONTROLLER_CHARM: METACONTROLLER_CHARM,
                },
            )
            extension = MetacontrollerExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN no new deploy happens, but the extension still waits for it to settle
            assert juju.deployed == []
            assert juju.waited_settled == [(TEST_MODEL.uri, METACONTROLLER_CHARM, "0:15:00")]

        def test_skips_deploy_when_metacontroller_already_deployed_under_a_different_application_name(
            self, logger: logging.Logger
        ) -> None:
            # GIVEN kfp-profile-controller is present and metacontroller-operator is already
            # deployed under a custom application name
            juju = JujuStub(
                applications={
                    "kfp-profile-controller": KFP_PROFILE_CONTROLLER_CHARM,
                    "my-metacontroller": METACONTROLLER_CHARM,
                },
            )
            extension = MetacontrollerExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN no new deploy happens; the extension waits on the existing application
            assert juju.deployed == []
            assert juju.waited_settled == [(TEST_MODEL.uri, "my-metacontroller", "0:15:00")]

        def test_ignores_models_on_non_k8s_controllers(self, logger: logging.Logger) -> None:
            # GIVEN kfp-profile-controller is present but the controller isn't backed by Kubernetes
            juju = JujuStub(applications={"kfp-profile-controller": KFP_PROFILE_CONTROLLER_CHARM}, is_k8s=False)
            extension = MetacontrollerExtension(juju, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN nothing is deployed
            assert juju.deployed == []
