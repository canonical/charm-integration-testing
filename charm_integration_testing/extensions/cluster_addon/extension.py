# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta

from juju import JujuBackend, JujuExtension, JujuModelHandle, JujuWaitTimeoutError

from bundle_builder_x import CharmChannel, ClusterAddonOverrides, OverridesClient

# Shared model hosting every "cluster"-scoped addon on a controller (one model, not
# one per addon, to avoid proliferation and stay easy to spot in `juju models`).
CLUSTER_ADDON_MODEL_NAME = "charm-integration-testing-cluster-addons"

DEFAULT_ADDON_MODEL_WAIT_TIMEOUT = timedelta(minutes=5)
DEFAULT_ADDON_DEPLOY_TIMEOUT = timedelta(minutes=15)


class ClusterAddonExtension(JujuExtension):
    """Deploys charms that another charm needs active but can't depend on via Juju relations.

    Some charms (e.g. istio-beacon-k8s) need a control-plane charm (e.g. istio-k8s)
    reconciling somewhere on the cluster to reach "active", with no Juju relation to
    express the dependency. Reads it from charm overrides instead (``cluster_addons``
    on the consumer, ``addon_scope`` on the addon) so it's declared, not hard-coded.

    "cluster" scope (default) deploys once per controller into a shared model
    (``CLUSTER_ADDON_MODEL_NAME``); "model" scope deploys into the dependent's own model.
    Deployment is idempotent: an already-existing model/application is treated as success.
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

        application = self._ensure_addon_deployed(target_model, addon)

        self.logger.info(f"Waiting for cluster addon '{addon.charm}' in '{target_model.uri}' to settle.")
        self.juju.wait_application_settled(target_model, application, timeout=DEFAULT_ADDON_DEPLOY_TIMEOUT)

    def _ensure_cluster_addon_model(self, controller: str) -> JujuModelHandle:
        addon_model = JujuModelHandle(model=CLUSTER_ADDON_MODEL_NAME, controller=controller)
        try:
            self.juju.add_model(controller=controller, model=CLUSTER_ADDON_MODEL_NAME, model_config={})
            self.logger.info(f"Created shared cluster addon model '{addon_model.uri}'.")
        except Exception as error:
            # Re-check authoritative state instead of pattern-matching backend errors,
            # to tolerate a concurrent worker having already created the model.
            try:
                self.juju.wait_for_model_to_exist(addon_model, timeout=DEFAULT_ADDON_MODEL_WAIT_TIMEOUT)
            except JujuWaitTimeoutError:
                raise error from None
        return addon_model

    def _find_addon_application(self, target_model: JujuModelHandle, charm: str) -> str | None:
        # Match by deployed charm, not application name -- an addon deployed under a
        # different app name must still count as satisfying the dependency.
        for application, info in self.juju.list_applications(target_model).items():
            if info.charm == charm:
                return application
        return None

    def _ensure_addon_deployed(self, target_model: JujuModelHandle, addon: ClusterAddonOverrides) -> str:
        existing = self._find_addon_application(target_model, addon.charm)
        if existing is not None:
            return existing
        try:
            self.juju.deploy_application(
                target_model, addon.charm, application=addon.charm, trust=True, channel=addon.channel
            )
            self.logger.info(f"Deployed cluster addon '{addon.charm}' into '{target_model.uri}'.")
            return addon.charm
        except Exception as error:
            # Re-check authoritative state instead of pattern-matching backend errors,
            # to tolerate a concurrent worker having already deployed the same addon.
            existing = self._find_addon_application(target_model, addon.charm)
            if existing is None:
                raise error from None
            return existing
