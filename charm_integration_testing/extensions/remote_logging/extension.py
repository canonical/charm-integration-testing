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
CONTROLLER_APPLICATION = "controller"
JUJU_INFO_ENDPOINT = "juju-info"

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
            self.logger.debug(f"Skipping remote logging setup for controller '{controller}': no endpoint configured.")
            return

        model = JujuModelHandle(controller=controller, model="controller")
        self.logger.debug(f"Starting remote logging setup for controller model '{model.uri}'.")
        self._log_status(model, phase="before")

        try:
            collector_charm = (
                KUBERNETES_COLLECTOR_CHARM if self.juju.is_k8s_controller(controller) else MACHINE_COLLECTOR_CHARM
            )
            self.logger.debug(f"Selected collector charm '{collector_charm}' for controller model '{model.uri}'.")

            applications = self.juju.list_applications(model)
            self.logger.debug(f"Found {len(applications)} application(s) in controller model '{model.uri}'.")

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
                self.logger.debug(f"Updating configuration for existing application '{integrator}' in '{model.uri}'.")
                self.juju.configure_application(
                    model,
                    integrator,
                    {
                        key: str(value).lower() if isinstance(value, bool) else value
                        for key, value in integrator_config.items()
                    },
                )

            if collector_charm == MACHINE_COLLECTOR_CHARM:
                if self.juju.integration_exists(
                    collector,
                    JUJU_INFO_ENDPOINT,
                    CONTROLLER_APPLICATION,
                    JUJU_INFO_ENDPOINT,
                    model,
                ):
                    self.logger.debug(
                        f"Reusing integration between '{collector}' and '{CONTROLLER_APPLICATION}' in '{model.uri}'."
                    )
                else:
                    self.logger.debug(f"Integrating '{collector}' with '{CONTROLLER_APPLICATION}' in '{model.uri}'.")
                    self.juju.integrate(
                        model,
                        JujuIntegrationApplication(collector, JUJU_INFO_ENDPOINT),
                        JujuIntegrationApplication(CONTROLLER_APPLICATION, JUJU_INFO_ENDPOINT),
                    )

            if self.juju.integration_exists(
                integrator,
                EXTERNAL_CONFIG_ENDPOINT,
                collector,
                EXTERNAL_CONFIG_ENDPOINT,
                model,
            ):
                self.logger.debug(f"Reusing integration between '{integrator}' and '{collector}' in '{model.uri}'.")
            else:
                self.logger.debug(f"Integrating '{integrator}' with '{collector}' in '{model.uri}'.")
                self.juju.integrate(
                    model,
                    JujuIntegrationApplication(integrator, EXTERNAL_CONFIG_ENDPOINT),
                    JujuIntegrationApplication(collector, EXTERNAL_CONFIG_ENDPOINT),
                )

            for application in (collector, integrator):
                self.logger.debug(f"Waiting for application '{application}' to settle in '{model.uri}'.")
                self.juju.wait_application_settled(
                    model,
                    application,
                    timeout=DEFAULT_REMOTE_LOGGING_DEPLOY_TIMEOUT,
                )
                self.logger.debug(f"Application '{application}' settled in '{model.uri}'.")
        except Exception:
            self.logger.debug(f"Remote logging setup failed for controller model '{model.uri}'.", exc_info=True)
            raise
        else:
            self.logger.debug(f"Completed remote logging setup for controller model '{model.uri}'.")
        finally:
            self._log_status(model, phase="after")

    def _log_status(self, model: JujuModelHandle, *, phase: str) -> None:
        try:
            status = self.juju.juju_status_text(model)
        except Exception:
            self.logger.warning(
                f"Failed to collect Juju status {phase} remote logging changes for controller model '{model.uri}'.",
                exc_info=True,
            )
            return

        separator = "-" * 80
        self.logger.info(
            f"Juju status {phase} remote logging changes for controller model '{model.uri}':\n"
            f"{separator}\n{status}{separator}"
        )

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
            self.logger.debug(f"Reusing application '{existing}' for charm '{charm}' in '{model.uri}'.")
            return existing

        self.logger.debug(f"Deploying charm '{charm}' as application '{charm}' in '{model.uri}'.")
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
            self.logger.debug(
                f"Deployment of charm '{charm}' in '{model.uri}' raised; checking for a concurrent deployment.",
                exc_info=True,
            )
            existing = self._find_application(self.juju.list_applications(model), charm)
            if existing is None:
                raise error from None
            self.logger.debug(
                f"Using concurrently deployed application '{existing}' for charm '{charm}' in '{model.uri}'."
            )
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
