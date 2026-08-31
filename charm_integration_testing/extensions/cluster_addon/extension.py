# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta

from juju import JujuBackend, JujuExtension, JujuModelHandle, JujuWaitTimeoutError

from bundle_builder_x import CharmChannel, ClusterAddonOverrides, OverridesClient

# Shared model name used to host every "cluster"-scoped addon on a given controller.
# Deliberately a single model (not one per addon charm) to avoid proliferating models
# for every addon and to make it easy to spot in `juju models` output.
CLUSTER_ADDON_MODEL_NAME = "charm-integration-testing-cluster-addons"

DEFAULT_ADDON_MODEL_WAIT_TIMEOUT = timedelta(minutes=5)
DEFAULT_ADDON_DEPLOY_TIMEOUT = timedelta(minutes=15)


class ClusterAddonExtension(JujuExtension):
    """Deploys charms that some other charm needs active but cannot depend on via Juju relations.

    Some charms (e.g. istio-beacon-k8s) rely on a control-plane charm being deployed and
    reconciling somewhere on the cluster (e.g. istio-k8s) in order to leave "maintenance"
    and reach "active", yet have no Juju relation to that charm -- the dependency is purely
    an operational/cluster-addon one, invisible to the bundle-builder's relation-based Z3
    solver. This extension reads such dependencies from charm overrides
    (``cluster_addons`` on the consumer's override, ``addon_scope`` on the addon's own
    override) so they are documented and auditable rather than hard-coded per charm.

    Addons are deployed either:
      - "cluster" scope (the default): once per controller, into a single shared model
        (``CLUSTER_ADDON_MODEL_NAME``) reused across every model on that controller, or
      - "model" scope: directly into the same model as the dependent charm.

    Deployment is idempotent: if the shared model or the addon application already exists
    (e.g. created by a concurrent test worker, or left over from a previous run), this is
    treated as success rather than an error.
    """

    juju: JujuBackend
    overrides: OverridesClient
    logger: logging.Logger

    def __init__(self, juju: JujuBackend, overrides: OverridesClient, logger: logging.Logger) -> None:
        self.juju = juju
        self.overrides = overrides
        self.logger = logger

    def post_deploy(self, model: JujuModelHandle) -> None:
        applications = self.juju.list_applications(model)
        for application, info in applications.items():
            if info.channel is None:
                continue
            channel = CharmChannel.model_validate(str(info.channel))
            for addon in self.overrides.get_charm_cluster_addons(info.charm, channel):
                self.logger.info(
                    f"Application '{application}' (charm '{info.charm}') requires cluster addon '{addon.charm}'."
                )
                self._ensure_addon(model, addon)

    def _ensure_addon(self, model: JujuModelHandle, addon: ClusterAddonOverrides) -> None:
        scope = self.overrides.get_charm_addon_scope(addon.charm)
        if scope == "model":
            target_model = model
        else:
            target_model = self._ensure_cluster_addon_model(model.controller)

        self._ensure_addon_deployed(target_model, addon)

        self.logger.info(f"Waiting for cluster addon '{addon.charm}' in '{target_model.uri}' to settle.")
        self.juju.wait_application_settled(target_model, addon.charm, timeout=DEFAULT_ADDON_DEPLOY_TIMEOUT)

    def _ensure_cluster_addon_model(self, controller: str) -> JujuModelHandle:
        addon_model = JujuModelHandle(model=CLUSTER_ADDON_MODEL_NAME, controller=controller)
        try:
            self.juju.add_model(controller=controller, model=CLUSTER_ADDON_MODEL_NAME, model_config={})
            self.logger.info(f"Created shared cluster addon model '{addon_model.uri}'.")
        except Exception as error:
            # Tolerate a concurrent test worker on the same controller having already
            # created the model. Re-check the authoritative state rather than pattern
            # matching on a specific backend's error message.
            try:
                self.juju.wait_for_model_to_exist(addon_model, timeout=DEFAULT_ADDON_MODEL_WAIT_TIMEOUT)
            except JujuWaitTimeoutError:
                raise error from None
        return addon_model

    def _ensure_addon_deployed(self, target_model: JujuModelHandle, addon: ClusterAddonOverrides) -> None:
        if addon.charm in self.juju.list_applications(target_model):
            return
        try:
            self.juju.deploy_application(
                target_model, addon.charm, application=addon.charm, trust=True, channel=addon.channel
            )
            self.logger.info(f"Deployed cluster addon '{addon.charm}' into '{target_model.uri}'.")
        except Exception as error:
            # Tolerate a concurrent test worker having already deployed the same addon
            # into the shared model. Re-check the authoritative state rather than pattern
            # matching on a specific backend's error message.
            if addon.charm not in self.juju.list_applications(target_model):
                raise error from None
