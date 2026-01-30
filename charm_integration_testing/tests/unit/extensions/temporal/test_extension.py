# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from extensions.temporal.extension import (
    TemporalExtension,
)
from juju.backend import JujuBackend, JujuTask

from ..shared import JujuStub as JujuStubBase


@dataclass
class JujuStub(JujuStubBase):
    applications: dict[str, str] = field(default_factory=dict)  # {"temporal-app": "temporal"})
    unit_ips: dict[str, str] = field(default_factory=dict)  # {"temporal-app/leader": "10.0.0.1"})
    action_responses: list[JujuTask] = field(default_factory=list)

    def run_action(self, model: str, unit: str, action: str, params: dict[str, Any]) -> JujuTask:
        """Mock running an action on a unit (captures call for verification)"""
        super().run_action(model, unit, action, params)
        return self.action_responses.pop(0)


class TestTemporalExtension:
    @pytest.fixture
    def juju(self) -> JujuStub:
        return JujuStub()

    @pytest.fixture
    def extension(self, juju: JujuBackend) -> TemporalExtension:
        return TemporalExtension(juju, logging.getLogger("test"))

    class TestPostDeploy:
        test_model = "test-model"

        def test_deploys_temporal_if_app_implicitly_requiring_it_is_present(
            self, extension: TemporalExtension, juju: JujuStub
        ) -> None:
            # Known example case: airbyte-k8s
            charm_name = "airbyte-k8s"
            app_name = "airbyte-app"

            # Prepping some bogus responses for juju actions which will be fired:
            # 1. A check for existing namespaces.  Empty output means it won't find the default namespace that the
            #    extension looks for.
            # 2. Creation of the default namespace.  As long as the response has status="completed", the code doesn't
            #    care.
            juju.action_responses.append(
                JujuTask(id="1", return_code=0, status="completed", message="message", output="")
            )
            juju.action_responses.append(
                JujuTask(id="1", return_code=0, status="completed", message="message", output="")
            )

            # Also, prepping some config for the airbyte-k8s app to mock its default config pointing to temporal
            juju.configured_applications.append((self.test_model, app_name, {"temporal-host": "temporal-k8s:7233"}))

            # GIVEN a model with an airbyte-k8s application
            juju.applications = {app_name: charm_name}
            # WHEN post_deploy is called
            extension.post_deploy(self.test_model)

            # THEN temporal is deployed
            assert (self.test_model, "temporal-k8s", None) in juju.deployed
            assert (self.test_model, "temporal-admin-k8s", None) in juju.deployed
            assert (self.test_model, "postgresql-k8s", None) in juju.deployed

            # ...Also, let's verify the actions we expect to have been called.
            assert len(juju.actions) >= 2, "Expected at least two actions to have been run"
            # First action is to check existing namespaces
            assert juju.actions[0][2] == "tctl"
            assert juju.actions[0][3] == {"args": "namespace list"}
            # Second action is namespace creation
            assert juju.actions[1][2] == "tctl"
            assert juju.actions[1][3] == {"args": "--ns default namespace register -rd 3"}

        def test_ignores_apps_without_known_temporal_dependency(self, juju: JujuStub) -> None:
            # GIVEN a model with no airbyte-k8s applications
            juju.applications = {"non-airbyte": "not-airbyte"}
            extension = TemporalExtension(juju, logging.getLogger("test"))

            # WHEN post_deploy is called
            extension.post_deploy(self.test_model)
            # THEN no deployments happen
            assert juju.deployed == []
