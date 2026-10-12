# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta
from typing import Any

import yaml
from juju import JujuApplicationInfo, JujuBackend, JujuExtension, JujuIntegrationApplication, JujuModelHandle

KUBERNETES_COLLECTOR_CHARM = "opentelemetry-collector-k8s"
MACHINE_COLLECTOR_CHARM = "opentelemetry-collector"
INTEGRATOR_CHARM = "opentelemetry-collector-integrator"
EXTERNAL_CONFIG_ENDPOINT = "external-config"

COLLECTOR_CHANNEL = "dev/edge"
INTEGRATOR_CHANNEL = "3.0/stable"

DEFAULT_REMOTE_LOGGING_DEPLOY_TIMEOUT = timedelta(minutes=15)


class RemoteLoggingExtension(JujuExtension):
    """Deploy a remote logging stack into configured controller models after bootstrap."""

    def __init__(
        self,
        juju: JujuBackend,
        logger: logging.Logger,
        endpoints_by_controller: dict[str, str],
    ) -> None:
        self.juju = juju
        self.logger = logger
        self.endpoints_by_controller = endpoints_by_controller

    def post_bootstrap_controller(self, controller: str) -> None:
        endpoint = self.endpoints_by_controller.get(controller)
        if endpoint is None:
            return

        model = JujuModelHandle(controller=controller, model="controller")
        collector_charm = (
            KUBERNETES_COLLECTOR_CHARM if self.juju.is_k8s_controller(controller) else MACHINE_COLLECTOR_CHARM
        )
        applications = self.juju.list_applications(model)

        collector = self._ensure_deployed(
            model,
            applications,
            charm=collector_charm,
            config=None,
            trust=collector_charm == KUBERNETES_COLLECTOR_CHARM,
            channel=COLLECTOR_CHANNEL,
        )
        integrator_config = self._integrator_config(endpoint)
        integrator = self._ensure_deployed(
            model,
            applications,
            charm=INTEGRATOR_CHARM,
            config=integrator_config,
            trust=False,
            channel=INTEGRATOR_CHANNEL,
        )

        if integrator in applications:
            self.juju.configure_application(
                model,
                integrator,
                {
                    key: str(value).lower() if isinstance(value, bool) else value
                    for key, value in integrator_config.items()
                },
            )

        if not self.juju.integration_exists(
            integrator,
            EXTERNAL_CONFIG_ENDPOINT,
            collector,
            EXTERNAL_CONFIG_ENDPOINT,
            model,
        ):
            self.juju.integrate(
                model,
                JujuIntegrationApplication(integrator, EXTERNAL_CONFIG_ENDPOINT),
                JujuIntegrationApplication(collector, EXTERNAL_CONFIG_ENDPOINT),
            )

        self.juju.wait_application_settled(model, collector, timeout=DEFAULT_REMOTE_LOGGING_DEPLOY_TIMEOUT)
        self.juju.wait_application_settled(model, integrator, timeout=DEFAULT_REMOTE_LOGGING_DEPLOY_TIMEOUT)

    def _ensure_deployed(
        self,
        model: JujuModelHandle,
        applications: dict[str, JujuApplicationInfo],
        *,
        charm: str,
        config: dict[str, Any] | None,
        trust: bool,
        channel: str,
    ) -> str:
        existing = self._find_application(applications, charm)
        if existing is not None:
            return existing

        try:
            self.juju.deploy_application(
                model,
                charm,
                application=charm,
                config=config,
                trust=trust,
                channel=channel,
            )
            self.logger.info(f"Deployed '{charm}' into '{model.uri}'.")
            return charm
        except Exception as error:
            existing = self._find_application(self.juju.list_applications(model), charm)
            if existing is None:
                raise error from None
            return existing

    @staticmethod
    def _find_application(applications: dict[str, JujuApplicationInfo], charm: str) -> str | None:
        for application, info in applications.items():
            if info.charm == charm:
                return application
        return None

    @staticmethod
    def _integrator_config(endpoint: str) -> dict[str, Any]:
        config_yaml = yaml.safe_dump(
            {
                "receivers": {
                    "syslog": {
                        "tcp": {"listen_address": "0.0.0.0:5424"},
                        "protocol": "rfc5424",
                    }
                },
                "exporters": {
                    "otlp": {
                        "endpoint": endpoint,
                        "tls": {"insecure": True},
                    }
                },
            },
            sort_keys=False,
        )
        return {
            "config_yaml": config_yaml,
            "metrics_pipeline": False,
            "traces_pipeline": False,
            "logs_pipeline": True,
        }
