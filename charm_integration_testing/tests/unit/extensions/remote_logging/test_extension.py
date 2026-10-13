# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
import yaml
from extensions.remote_logging.extension import (
    CONTROLLER_APPLICATION,
    DEFAULT_REMOTE_LOGGING_DEPLOY_TIMEOUT,
    EXTERNAL_CONFIG_ENDPOINT,
    INTEGRATOR_CHARM,
    JUJU_INFO_ENDPOINT,
    KUBERNETES_COLLECTOR_CHARM,
    MACHINE_COLLECTOR_CHARM,
    RemoteLoggingExtension,
)
from juju import JujuModelHandle, JujuWaitTimeoutError

from ..shared import JujuStub as JujuStubBase

CONTROLLER = "test-controller"
CONTROLLER_MODEL = JujuModelHandle(controller=CONTROLLER, model="controller")
OTLP_ENDPOINT = "observability.example:4317"


@dataclass
class JujuStub(JujuStubBase):
    deployments: list[tuple[str, str, str | None, dict[str, Any] | None, bool]] = field(default_factory=list)
    deployment_error_for: str | None = None
    wait_error_for: str | None = None
    status_error_calls: set[int] = field(default_factory=set)
    status_models: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

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
        self.events.append(f"deploy:{charm}")
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

    def wait_application_settled(
        self,
        model: JujuModelHandle,
        application: str,
        timeout: timedelta | None,
    ) -> None:
        self.events.append(f"wait:{application}")
        super().wait_application_settled(model, application, timeout)
        if application == self.wait_error_for:
            raise JujuWaitTimeoutError()

    def juju_status_text(self, model: JujuModelHandle) -> str:
        call = len(self.status_models) + 1
        self.status_models.append(model.uri)
        self.events.append("status")
        if call in self.status_error_calls:
            raise RuntimeError(f"status call {call} failed")
        return "controller status\n"


class TestRemoteLoggingExtension:
    @pytest.fixture
    def logger(self) -> logging.Logger:
        return logging.getLogger("test")

    def test_skips_controller_without_endpoint(
        self,
        caplog: pytest.LogCaptureFixture,
        logger: logging.Logger,
    ) -> None:
        # GIVEN no remote logging endpoint for this controller
        juju = JujuStub()
        extension = RemoteLoggingExtension(juju, logger, {})

        # WHEN the controller is bootstrapped
        with caplog.at_level(logging.DEBUG):
            extension.post_bootstrap_controller(CONTROLLER)

        # THEN no controller-model operations are performed
        assert juju.deployments == []
        assert juju.integrations == []
        assert juju.waited_settled == []
        assert juju.status_models == []
        assert f"Skipping remote logging setup for controller '{CONTROLLER}'" in caplog.text

    @pytest.mark.parametrize(
        ("is_k8s", "collector_charm", "collector_trust"),
        [
            pytest.param(True, KUBERNETES_COLLECTOR_CHARM, True, id="kubernetes"),
            pytest.param(False, MACHINE_COLLECTOR_CHARM, False, id="machine"),
        ],
    )
    def test_deploys_configures_integrates_and_waits(
        self,
        caplog: pytest.LogCaptureFixture,
        logger: logging.Logger,
        is_k8s: bool,
        collector_charm: str,
        collector_trust: bool,
    ) -> None:
        # GIVEN a configured controller with no existing telemetry applications
        juju = JujuStub(is_k8s=is_k8s)
        extension = RemoteLoggingExtension(juju, logger, {CONTROLLER: OTLP_ENDPOINT})

        # WHEN the controller is bootstrapped
        with caplog.at_level(logging.DEBUG):
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
            "receivers": {
                "syslog": {
                    "tcp": {"listen_address": "0.0.0.0:5424"},
                    "protocol": "rfc5424",
                }
            },
            "exporters": {
                "otlp": {
                    "endpoint": OTLP_ENDPOINT,
                    "tls": {"insecure": True},
                }
            },
        }
        expected_integrations = []
        if not is_k8s:
            expected_integrations.append(
                (
                    CONTROLLER_MODEL.uri,
                    collector_charm,
                    JUJU_INFO_ENDPOINT,
                    CONTROLLER_APPLICATION,
                    JUJU_INFO_ENDPOINT,
                )
            )
        expected_integrations.append(
            (
                CONTROLLER_MODEL.uri,
                INTEGRATOR_CHARM,
                EXTERNAL_CONFIG_ENDPOINT,
                collector_charm,
                EXTERNAL_CONFIG_ENDPOINT,
            )
        )
        assert juju.integrations == expected_integrations
        timeout = str(DEFAULT_REMOTE_LOGGING_DEPLOY_TIMEOUT)
        assert juju.waited_settled == [
            (CONTROLLER_MODEL.uri, CONTROLLER_APPLICATION, timeout),
            (CONTROLLER_MODEL.uri, collector_charm, timeout),
            (CONTROLLER_MODEL.uri, INTEGRATOR_CHARM, timeout),
        ]
        assert juju.status_models == [CONTROLLER_MODEL.uri, CONTROLLER_MODEL.uri]
        assert juju.events == [
            "status",
            f"wait:{CONTROLLER_APPLICATION}",
            f"deploy:{collector_charm}",
            f"deploy:{INTEGRATOR_CHARM}",
            f"wait:{collector_charm}",
            f"wait:{INTEGRATOR_CHARM}",
            "status",
        ]
        assert f"Juju status before remote logging changes for controller model '{CONTROLLER_MODEL.uri}'" in caplog.text
        assert f"Juju status after remote logging changes for controller model '{CONTROLLER_MODEL.uri}'" in caplog.text
        assert "controller status" in caplog.text
        assert f"Selected collector charm '{collector_charm}'" in caplog.text
        assert f"Waiting for application '{collector_charm}' to settle" in caplog.text
        assert f"Completed remote logging setup for controller model '{CONTROLLER_MODEL.uri}'" in caplog.text

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
                        "receivers:\n"
                        "  syslog:\n"
                        "    tcp:\n"
                        "      listen_address: 0.0.0.0:5424\n"
                        "    protocol: rfc5424\n"
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

    def test_reuses_existing_machine_collector_relations(self, logger: logging.Logger) -> None:
        # GIVEN the machine stack and both required relations already exist
        juju = JujuStub(
            is_k8s=False,
            applications={
                "collector": MACHINE_COLLECTOR_CHARM,
                "integrator": INTEGRATOR_CHARM,
            },
        )
        juju.integrations.extend(
            [
                (
                    CONTROLLER_MODEL.uri,
                    "collector",
                    JUJU_INFO_ENDPOINT,
                    CONTROLLER_APPLICATION,
                    JUJU_INFO_ENDPOINT,
                ),
                (
                    CONTROLLER_MODEL.uri,
                    "integrator",
                    EXTERNAL_CONFIG_ENDPOINT,
                    "collector",
                    EXTERNAL_CONFIG_ENDPOINT,
                ),
            ]
        )
        extension = RemoteLoggingExtension(juju, logger, {CONTROLLER: OTLP_ENDPOINT})

        # WHEN the hook runs again
        extension.post_bootstrap_controller(CONTROLLER)

        # THEN neither relation is duplicated
        assert len(juju.integrations) == 2

    def test_propagates_deployment_failure_and_logs_final_status(self, logger: logging.Logger) -> None:
        # GIVEN the collector deployment fails and no concurrent deployment appears
        juju = JujuStub(deployment_error_for=KUBERNETES_COLLECTOR_CHARM)
        extension = RemoteLoggingExtension(juju, logger, {CONTROLLER: OTLP_ENDPOINT})

        # WHEN/THEN the failure is surfaced
        with pytest.raises(RuntimeError, match=f"failed to deploy {KUBERNETES_COLLECTOR_CHARM}"):
            extension.post_bootstrap_controller(CONTROLLER)

        # AND status is captured before and after the failed change
        assert juju.status_models == [CONTROLLER_MODEL.uri, CONTROLLER_MODEL.uri]
        assert juju.events == [
            "status",
            f"wait:{CONTROLLER_APPLICATION}",
            f"deploy:{KUBERNETES_COLLECTOR_CHARM}",
            "status",
        ]

    def test_propagates_wait_timeout_and_logs_final_status(self, logger: logging.Logger) -> None:
        # GIVEN the collector does not settle before the remote logging timeout
        juju = JujuStub(wait_error_for=KUBERNETES_COLLECTOR_CHARM)
        extension = RemoteLoggingExtension(juju, logger, {CONTROLLER: OTLP_ENDPOINT})

        # WHEN/THEN the timeout remains visible to the caller
        with pytest.raises(JujuWaitTimeoutError):
            extension.post_bootstrap_controller(CONTROLLER)

        # AND status is captured after the timeout without waiting for the integrator
        assert juju.status_models == [CONTROLLER_MODEL.uri, CONTROLLER_MODEL.uri]
        assert juju.waited_settled == [
            (CONTROLLER_MODEL.uri, CONTROLLER_APPLICATION, str(DEFAULT_REMOTE_LOGGING_DEPLOY_TIMEOUT)),
            (
                CONTROLLER_MODEL.uri,
                KUBERNETES_COLLECTOR_CHARM,
                str(DEFAULT_REMOTE_LOGGING_DEPLOY_TIMEOUT),
            ),
        ]
        assert juju.events[-2:] == [f"wait:{KUBERNETES_COLLECTOR_CHARM}", "status"]

    def test_status_failures_do_not_fail_successful_setup(
        self,
        caplog: pytest.LogCaptureFixture,
        logger: logging.Logger,
    ) -> None:
        # GIVEN both diagnostic status commands fail
        juju = JujuStub(status_error_calls={1, 2})
        extension = RemoteLoggingExtension(juju, logger, {CONTROLLER: OTLP_ENDPOINT})

        # WHEN the remote logging stack is configured
        with caplog.at_level(logging.WARNING):
            extension.post_bootstrap_controller(CONTROLLER)

        # THEN setup still completes and both diagnostic failures are logged
        assert len(juju.waited_settled) == 3
        assert juju.status_models == [CONTROLLER_MODEL.uri, CONTROLLER_MODEL.uri]
        assert caplog.text.count("Failed to collect Juju status") == 2

    def test_final_status_failure_does_not_mask_setup_failure(self, logger: logging.Logger) -> None:
        # GIVEN deployment and the final diagnostic status command both fail
        juju = JujuStub(
            deployment_error_for=KUBERNETES_COLLECTOR_CHARM,
            status_error_calls={2},
        )
        extension = RemoteLoggingExtension(juju, logger, {CONTROLLER: OTLP_ENDPOINT})

        # WHEN/THEN the original setup failure is preserved
        with pytest.raises(RuntimeError, match=f"failed to deploy {KUBERNETES_COLLECTOR_CHARM}"):
            extension.post_bootstrap_controller(CONTROLLER)

        assert juju.status_models == [CONTROLLER_MODEL.uri, CONTROLLER_MODEL.uri]

    def test_waits_for_controller_before_making_changes(self, logger: logging.Logger) -> None:
        # GIVEN the controller application does not settle
        juju = JujuStub(wait_error_for=CONTROLLER_APPLICATION)
        extension = RemoteLoggingExtension(juju, logger, {CONTROLLER: OTLP_ENDPOINT})

        # WHEN/THEN the timeout is surfaced
        with pytest.raises(JujuWaitTimeoutError):
            extension.post_bootstrap_controller(CONTROLLER)

        # AND nothing is deployed or integrated
        assert juju.events == ["status", f"wait:{CONTROLLER_APPLICATION}", "status"]
        assert juju.deployments == []
        assert juju.integrations == []
