# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
import yaml
from extensions.remote_logging.extension import (
    DEFAULT_REMOTE_LOGGING_DEPLOY_TIMEOUT,
    EXTERNAL_CONFIG_ENDPOINT,
    INTEGRATOR_CHARM,
    KUBERNETES_COLLECTOR_CHARM,
    MACHINE_COLLECTOR_CHARM,
    RemoteLoggingExtension,
)
from juju import JujuModelHandle

from ..shared import JujuStub as JujuStubBase

CONTROLLER = "test-controller"
CONTROLLER_MODEL = JujuModelHandle(controller=CONTROLLER, model="controller")
OTLP_ENDPOINT = "observability.example:4317"


@dataclass
class JujuStub(JujuStubBase):
    deployments: list[tuple[str, str, str | None, dict[str, Any] | None, bool]] = field(default_factory=list)
    deployment_error_for: str | None = None

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
        if charm == self.deployment_error_for:
            raise RuntimeError(f"failed to deploy {charm}")
        self.deployments.append((model.uri, charm, application, config, trust))
        super().deploy_application(
            model,
            charm,
            application=application,
            config=config,
            trust=trust,
            force=force,
            channel=channel,
        )


class TestRemoteLoggingExtension:
    @pytest.fixture
    def logger(self) -> logging.Logger:
        return logging.getLogger("test")

    def test_skips_controller_without_endpoint(self, logger: logging.Logger) -> None:
        # GIVEN no remote logging endpoint for this controller
        juju = JujuStub()
        extension = RemoteLoggingExtension(juju, logger, {})

        # WHEN the controller is bootstrapped
        extension.post_bootstrap_controller(CONTROLLER)

        # THEN no controller-model operations are performed
        assert juju.deployments == []
        assert juju.integrations == []
        assert juju.waited_settled == []

    @pytest.mark.parametrize(
        ("is_k8s", "collector_charm", "collector_trust"),
        [
            pytest.param(True, KUBERNETES_COLLECTOR_CHARM, True, id="kubernetes"),
            pytest.param(False, MACHINE_COLLECTOR_CHARM, False, id="machine"),
        ],
    )
    def test_deploys_configures_integrates_and_waits(
        self,
        logger: logging.Logger,
        is_k8s: bool,
        collector_charm: str,
        collector_trust: bool,
    ) -> None:
        # GIVEN a configured controller with no existing telemetry applications
        juju = JujuStub(is_k8s=is_k8s)
        extension = RemoteLoggingExtension(juju, logger, {CONTROLLER: OTLP_ENDPOINT})

        # WHEN the controller is bootstrapped
        extension.post_bootstrap_controller(CONTROLLER)

        # THEN the substrate-specific collector and configured integrator are deployed
        assert juju.deployments[0] == (CONTROLLER_MODEL.uri, collector_charm, collector_charm, None, collector_trust)
        integrator_deployment = juju.deployments[1]
        assert integrator_deployment[:3] == (CONTROLLER_MODEL.uri, INTEGRATOR_CHARM, INTEGRATOR_CHARM)
        assert integrator_deployment[4] is False
        assert integrator_deployment[3] is not None
        assert integrator_deployment[3]["metrics_pipeline"] is False
        assert integrator_deployment[3]["traces_pipeline"] is False
        assert integrator_deployment[3]["logs_pipeline"] is True
        assert yaml.safe_load(integrator_deployment[3]["config_yaml"]) == {
            "exporters": {
                "otlp": {
                    "endpoint": OTLP_ENDPOINT,
                    "tls": {"insecure": True},
                }
            }
        }
        assert juju.integrations == [
            (
                CONTROLLER_MODEL.uri,
                INTEGRATOR_CHARM,
                EXTERNAL_CONFIG_ENDPOINT,
                collector_charm,
                EXTERNAL_CONFIG_ENDPOINT,
            )
        ]
        timeout = str(DEFAULT_REMOTE_LOGGING_DEPLOY_TIMEOUT)
        assert juju.waited_settled == [
            (CONTROLLER_MODEL.uri, collector_charm, timeout),
            (CONTROLLER_MODEL.uri, INTEGRATOR_CHARM, timeout),
        ]

    def test_reuses_existing_applications_and_integration(self, logger: logging.Logger) -> None:
        # GIVEN the stack already exists under custom application names and is integrated
        juju = JujuStub(
            applications={
                "collector": KUBERNETES_COLLECTOR_CHARM,
                "integrator": INTEGRATOR_CHARM,
            }
        )
        juju.integrations.append(
            (
                CONTROLLER_MODEL.uri,
                "integrator",
                EXTERNAL_CONFIG_ENDPOINT,
                "collector",
                EXTERNAL_CONFIG_ENDPOINT,
            )
        )
        extension = RemoteLoggingExtension(juju, logger, {CONTROLLER: OTLP_ENDPOINT})

        # WHEN the hook runs again
        extension.post_bootstrap_controller(CONTROLLER)

        # THEN it updates the integrator config without redeploying or duplicating the relation
        assert juju.deployments == []
        assert len(juju.integrations) == 1
        assert juju.configured_applications == [
            (
                CONTROLLER_MODEL.uri,
                "integrator",
                {
                    "config_yaml": (
                        "exporters:\n"
                        "  otlp:\n"
                        f"    endpoint: {OTLP_ENDPOINT}\n"
                        "    tls:\n"
                        "      insecure: true\n"
                    ),
                    "metrics_pipeline": "false",
                    "traces_pipeline": "false",
                    "logs_pipeline": "true",
                },
            )
        ]

    def test_propagates_deployment_failure(self, logger: logging.Logger) -> None:
        # GIVEN the collector deployment fails and no concurrent deployment appears
        juju = JujuStub(deployment_error_for=KUBERNETES_COLLECTOR_CHARM)
        extension = RemoteLoggingExtension(juju, logger, {CONTROLLER: OTLP_ENDPOINT})

        # WHEN/THEN the failure is surfaced
        with pytest.raises(RuntimeError, match=f"failed to deploy {KUBERNETES_COLLECTOR_CHARM}"):
            extension.post_bootstrap_controller(CONTROLLER)
