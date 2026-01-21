# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import time
from abc import ABC
from datetime import timedelta
from typing import Any

from juju import JujuBackend, JujuExtension
from juju.backend import JujuIntegrationApplication


class TemporalExtension(JujuExtension, ABC):
    """Auto-deploys temporal to support charms with implicit temporal dependencies."""

    juju: JujuBackend
    logger: logging.Logger

    # Map of charm to respective config option to set for temporal endpoint
    CONFIG_MAP = {
        "airbyte-k8s": "temporal-host",
        "temporal-worker-k8s": "host",
    }

    TEMPORAL_WAIT_TIMEOUT = timedelta(minutes=15)
    BOOTSTRAP_MAX_RETRIES = 10
    BOOTSTRAP_RETRY_INTERVAL = 10  # seconds

    def __init__(self, juju: JujuBackend, logger: logging.Logger):
        self.juju = juju
        self.logger = logger

    def post_deploy(self, model: str) -> None:
        """Post-deploy hook to deploy temporal if necessary.

        :param model: The Juju model to deploy to.
        :type model: str
        """
        if self.should_deploy_temporal(model):
            self.deploy_temporal_stack(model)
            self.configure_dependent_charms(model)

    def should_deploy_temporal(self, model: str) -> bool:
        """Determine if temporal should be deployed in the given model.

        :param model: The Juju model to check.
        :type model: str
        :return: True if temporal should be deployed, False otherwise.
        :rtype: bool
        """
        for application in self.juju.list_applications(model):
            if self.juju.application_charm(model, application) in self.CONFIG_MAP:
                return True
        return False

    def deploy_temporal_stack(self, model: str) -> None:
        """Deploy the temporal stack in the given model.

        :param model: The Juju model to deploy to.
        :type model: str
        """
        required_charms_and_config: dict[str, dict[str, Any]] = {
            "temporal-k8s": {"num-history-shards": 4},
            "temporal-admin-k8s": {},
            "postgresql-k8s": {},
        }

        # Find required apps, or deploy them if necessary
        deployed_apps = {}
        for application in self.juju.list_applications(model):
            charm = self.juju.application_charm(model, application)
            if charm in required_charms_and_config:
                deployed_apps[charm] = application
        for charm, config in required_charms_and_config.items():
            if charm not in deployed_apps:
                self._log(f"Deploying {charm} to model {model}")
                self.juju.deploy_application(model, charm, config=config)
                deployed_apps[charm] = charm  # Assume application name is same as charm name

        # Set up relations (if not already related)
        relations = [
            (("temporal-k8s", "db"), ("postgresql-k8s", "database")),
            (("temporal-k8s", "visibility"), ("postgresql-k8s", "database")),
            (("temporal-k8s", "admin"), ("temporal-admin-k8s", "admin")),
        ]
        for (charm1, endpoint1), (charm2, endpoint2) in relations:
            app1 = deployed_apps[charm1]
            app2 = deployed_apps[charm2]
            if not self.juju.integration_exists(app1, endpoint1, app2, endpoint2, model):
                self._log(f"Relating {app1}:{endpoint1} to {app2}:{endpoint2}")
                self.juju.integrate(
                    model,
                    JujuIntegrationApplication(app1, endpoint1),
                    JujuIntegrationApplication(app2, endpoint2),
                )

        # Wait for the temporal sub-bundle to become active/idle.
        # Note: this uses jubilant for waiting, not "juju wait-for" which is going away,
        # so hopefully this doesn't break when that finally gets removed.
        apps_to_wait_for = list(deployed_apps.values())
        self._log(f"Waiting for temporal-related applications to settle: {apps_to_wait_for}")
        self.juju.wait_idle(model, applications=apps_to_wait_for, timeout=self.TEMPORAL_WAIT_TIMEOUT, count=3)

        # Bootstrap temporal if necessary
        temporal_admin_app = deployed_apps["temporal-admin-k8s"]
        if not self._is_default_temporal_namespace_bootstrapped(model, temporal_admin_app):
            self._bootstrap_default_temporal_namespace(model, temporal_admin_app)

    def _is_default_temporal_namespace_bootstrapped(self, model: str, temporal_admin_app: str) -> bool:
        # NOTE: tctl is apparently deprecated upstream, so this will likely need to change at some point
        # in the future.
        task = self.juju.run_action(
            model,
            f"{temporal_admin_app}/0",
            "tctl",
            {"args": "namespace list"},
        )
        for line in task.output.splitlines():
            if line.strip() == "Name: default":
                return True

        return False

    def _bootstrap_default_temporal_namespace(self, model: str, temporal_admin_app: str) -> None:
        def inner_logic() -> None:
            task = self.juju.run_action(
                model,
                f"{temporal_admin_app}/0",
                "tctl",
                {"args": "--ns default namespace register -rd 3"},
            )
            if task.status != "completed":
                raise RuntimeError("Failed to bootstrap temporal default namespace", task)

        # We'll wrap this in a retry loop just in case
        self._log("Bootstrapping temporal namespace 'default'")
        for i in range(self.BOOTSTRAP_MAX_RETRIES):
            try:
                inner_logic()
            except RuntimeError as e:
                if i < self.BOOTSTRAP_MAX_RETRIES - 1:
                    self._log(f"Temporal namespace bootstrap action failed on attempt {i + 1}: {e.args[1]}")
                    self._log(f"Retrying in {self.BOOTSTRAP_RETRY_INTERVAL} seconds...")
                    time.sleep(self.BOOTSTRAP_RETRY_INTERVAL)
                else:
                    raise
            else:
                break

    def configure_dependent_charms(self, model: str) -> None:
        """Configure charms that depend on temporal in the given model.

        :param model: The Juju model to configure.
        :type model: str
        """
        TEMPORAL_HOST = "temporal-k8s:7233"
        for charm in self.CONFIG_MAP:
            for application in self.juju.list_applications(model):
                if self.juju.application_charm(model, application) == charm:
                    config_option = self.CONFIG_MAP[charm]
                    if self.juju.get_application_config(model, application).get(config_option) != TEMPORAL_HOST:
                        self._log(f"Configuring {application} in model {model} to use temporal endpoint")
                        self.juju.configure_application(
                            model,
                            application,
                            {config_option: TEMPORAL_HOST},
                        )

    def _log(self, message: str) -> None:
        self.logger.info(f"TemporalExtension: {message}")
