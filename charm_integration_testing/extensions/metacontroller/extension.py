# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta

from juju import JujuApplicationInfo, JujuBackend, JujuExtension, JujuModelHandle

# Charms that create metacontroller.k8s.io/v1alpha1 resources (e.g. DecoratorControllers) at
# runtime, with no Juju relation to express the dependency on the CRDs that provide that API
# group. See canonical/charm-integration-testing#474 and canonical/kfp-operators#876: without
# the CRDs, kfp-profile-controller's own reconciliation loop hits an uncaught 404 and the unit
# blocks with "Failed to compute status.  See logs for details."
METACONTROLLER_DEPENDENT_CHARMS = frozenset({"kfp-profile-controller"})
METACONTROLLER_CHARM = "metacontroller-operator"
# Matches the channel kfp-operators' own reference bundle.yaml deploys metacontroller-operator
# on: https://github.com/canonical/kfp-operators/blob/main/bundle.yaml
METACONTROLLER_CHANNEL = "latest/edge"

DEFAULT_METACONTROLLER_DEPLOY_TIMEOUT = timedelta(minutes=15)


class MetacontrollerExtension(JujuExtension):
    """Deploys metacontroller-operator into a model when a metacontroller-dependent charm is
    present but has no metacontroller CRDs of its own to reconcile against.

    Deployment is idempotent: an already-deployed metacontroller-operator application in this model
    is treated as success, and is only ever attempted once even if multiple dependents are present.
    """

    juju: JujuBackend
    logger: logging.Logger

    def __init__(self, juju: JujuBackend, logger: logging.Logger) -> None:
        self.juju = juju
        self.logger = logger

    def post_deploy(self, model: JujuModelHandle) -> None:
        applications = self.juju.list_applications(model)
        dependents = sorted(
            info.charm for info in applications.values() if info.charm in METACONTROLLER_DEPENDENT_CHARMS
        )
        if not dependents:
            return

        if self.juju.get_kubernetes_client_for_controller(model.controller) is None:
            return

        existing = self._find_metacontroller_application(applications)
        if existing is not None:
            self.juju.wait_application_settled(model, existing, timeout=DEFAULT_METACONTROLLER_DEPLOY_TIMEOUT)
            return

        self.logger.info(
            f"{dependents} present in '{model.uri}' with no '{METACONTROLLER_CHARM}' of its own; "
            f"deploying '{METACONTROLLER_CHARM}'."
        )
        application = self._ensure_metacontroller_deployed(model)
        self.juju.wait_application_settled(model, application, timeout=DEFAULT_METACONTROLLER_DEPLOY_TIMEOUT)

    def _ensure_metacontroller_deployed(self, model: JujuModelHandle) -> str:
        existing = self._find_metacontroller_application(self.juju.list_applications(model))
        if existing is not None:
            return existing
        try:
            self.juju.deploy_application(
                model,
                METACONTROLLER_CHARM,
                application=METACONTROLLER_CHARM,
                trust=True,
                channel=METACONTROLLER_CHANNEL,
            )
            self.logger.info(f"Deployed '{METACONTROLLER_CHARM}' into '{model.uri}'.")
            return METACONTROLLER_CHARM
        except Exception as error:
            # Re-check authoritative state instead of pattern-matching backend errors, to
            # tolerate a concurrent worker having already deployed metacontroller-operator.
            existing = self._find_metacontroller_application(self.juju.list_applications(model))
            if existing is None:
                raise error from None
            return existing

    @staticmethod
    def _find_metacontroller_application(applications: dict[str, JujuApplicationInfo]) -> str | None:
        for application, info in applications.items():
            if info.charm == METACONTROLLER_CHARM:
                return application
        return None
