# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from abc import ABC
from typing import Any, Mapping

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

    def __init__(self, juju: JujuBackend, logger: logging.Logger):
        self.juju = juju
        self.logger = logger

    def post_deploy(self, model: str):
        """
        Docstring for post_deploy
        
        :param self: Description
        :param model: Description
        :type model: str
        """

        """
        Pseudo-code:
        * What triggers this extension?
          * airbyte-k8s charm presense: implicit dependency on temporal
            * Lacks a relation to temporal
            * Does have a config option for temporal endpoint
          * Other charms?
            * temporal-worker-k8s mentioned.  How would it relate?
              * Also no relation to temporal
              * But it does have a "host" config option.
          So, for initial implementation:
            * Mapping from charm name to config option for temporal endpoint:
                * airbyte-k8s -> "temporal-host"
                * temporal-worker-k8s -> "host"
            * For airbyte-k8s: config targets temporal-k8s:7233; we just need to deploy that app and it'll link up.
            * For temporal-worker-k8s: no default specified.  Assuming above constraint, just set the host to temporal-k8s:7233.
        * Upon detection, what happens?
          * Look for the following charms and note their application names.  For any charms not found, deploy them with default names:
            * temporal-k8s (note: requires --config num-history-shards=4)
            * temporal-admin-k8s
            * postgresql-k8s
          * Set up relations if not present:
            * temporal-k8s:db         -> postgresql-k8s:database
            * temporal-k8s:visibility -> postgresql-k8s:database
            * temporal-k8s:admin      -> temporal-admin-k8s:admin
          * Wait for all applications to become active/idle
          * Bootstrap temporal-k8s, if necessary:
            * Check for if namespace is already registered:
                juju run temporal-admin-k8s/0 tctl args="--ns default namespace describe"  #  Is this real?  Double-check...
            * If not registered, run:
                juju run temporal-admin-k8s/0 tctl args="--ns default namespace register -rd 3"
          * Set the config option on the detected charms to point to temporal:
            * airbyte-k8s: temporal-host -> temporal-k8s:7233  (default, so no need to *actually* set this)
            * temporal-worker-k8s: host -> temporal-k8s:7233   (required; there is no default)
          * Should we handle workarounds for known issues?
            ...Not for airbyte-k8s.  This extension is intended to be generic.
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

    def deploy_temporal_stack(self, model: str):
        """Deploy the temporal stack in the given model.

        :param model: The Juju model to deploy to.
        :type model: str
        """
        # Pseudo-code implementation
        # Find or deploy temporal-k8s, temporal-admin-k8s, postgresql-k8s
        # Set up necessary relations if not present
        # Wait for the temporal-related applications specifically to become active/idle
        # Bootstrap temporal if necessary
        #
        # This is roughly equivalent to the following CLI actions (assuming we have to deploy everything):
        # juju deploy temporal-k8s --config num-history-shards=4
        # juju deploy temporal-admin-k8s
        # juju deploy postgresql-k8s
        # juju relate temporal-k8s:db postgresql-k8s:database
        # juju relate temporal-k8s:visibility postgresql-k8s:database
        # juju relate temporal-k8s:admin temporal-admin-k8s:admin
        # juju wait --for application=temporal-k8s,temporal-admin-k8s,postgresql-k8s  # Until active/idle
        # juju run temporal-admin-k8s/0 tctl args="--ns default namespace describe"
        # (if not registered)
        # juju run temporal-admin-k8s/0 tctl args="--ns default namespace register -rd 3"
        required_charms_and_config: Mapping[str, Mapping[str, Any]] = {
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

        return  # Disable remaining code for now

        # Wait for the terraform sub-bundle to become active/idle.
        # Note: this uses jubilant for waiting, not "juju wait-for" which is going away,
        # so hopefully this doesn't break when that finally gets removed.
        for app in deployed_apps.values():
            self._log(f"Waiting for application '{app}' to become active/idle")
            self.juju.wait_application_settled(model, app, timeout=None)  # Use default timeout

        # Bootstrap temporal if necessary
        temporal_admin_app = deployed_apps["temporal-admin-k8s"]
        namespace_registered = self.juju.run_command(
            model,
            f"{temporal_admin_app}/0",
            "tctl args='--ns default namespace describe'",
        )
        if namespace_registered.exit_code != 0:
            self._log("Bootstrapping temporal namespace 'default'")
            self.juju.run_command(
                model,
                f"{temporal_admin_app}/0",
                "tctl args='--ns default namespace register -rd 3'",
            )

        # Unsure: wait again?

    def configure_dependent_charms(self, model: str):
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

    def _log(self, message: str):
        self.logger.info(f"TemporalExtension: {message}")
