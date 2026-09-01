# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta

from juju import JujuApplicationInfo, JujuBackend, JujuExtension, JujuModelHandle

# Charms that need a reconciling istio-k8s control plane to reach "active", with no Juju
# relation to express the dependency -- both query the Gateway API CRDs directly.
ISTIO_MESH_DEPENDENT_CHARMS = frozenset({"istio-beacon-k8s", "istio-ingress-k8s"})
ISTIO_CONTROL_PLANE_CHARM = "istio-k8s"
ISTIO_CONTROL_PLANE_CHANNEL = "1/stable"
# Gateway API CRD istio-k8s installs; dependents 404 on config-changed without it (root
# cause of the istio-beacon-k8s rev 74, 1/stable hang -- reproduced against test_execution
# 656938 -- and the same class of failure for istio-ingress-k8s).
GATEWAY_CRD_NAME = "gateways.gateway.networking.k8s.io"

DEFAULT_ISTIO_DEPLOY_TIMEOUT = timedelta(minutes=15)


class IstioMeshExtension(JujuExtension):
    """Deploys istio-k8s when a mesh-dependent charm is present but its CRDs are missing.

    istio-beacon-k8s and istio-ingress-k8s each need a reconciling istio-k8s control plane
    to reach "active", with no Juju relation to express the dependency. Checked directly
    against cluster state (the Gateway API CRD istio-k8s installs) rather than inferred
    from bundle content, so a cluster that already has a working mesh is left alone.
    Deployment is idempotent: an already-deployed istio-k8s application is treated as
    success, and is only ever attempted once even if multiple dependents are present.
    """

    juju: JujuBackend
    logger: logging.Logger

    def __init__(self, juju: JujuBackend, logger: logging.Logger) -> None:
        self.juju = juju
        self.logger = logger

    def post_deploy(self, model: JujuModelHandle) -> None:
        applications = self.juju.list_applications(model)
        dependents = sorted(info.charm for info in applications.values() if info.charm in ISTIO_MESH_DEPENDENT_CHARMS)
        if not dependents:
            return

        kubernetes_client = self.juju.get_kubernetes_client_for_controller(model.controller)
        if kubernetes_client is None:
            return

        if kubernetes_client.crd_exists(GATEWAY_CRD_NAME):
            self.logger.info(f"Gateway API CRD '{GATEWAY_CRD_NAME}' already present; not deploying istio-k8s.")
            return

        self.logger.info(
            f"{dependents} present in '{model.uri}' but '{GATEWAY_CRD_NAME}' CRD is missing; "
            f"deploying '{ISTIO_CONTROL_PLANE_CHARM}'."
        )
        application = self._ensure_istio_deployed(model)
        self.juju.wait_application_settled(model, application, timeout=DEFAULT_ISTIO_DEPLOY_TIMEOUT)

    def _ensure_istio_deployed(self, model: JujuModelHandle) -> str:
        existing = self._find_istio_application(self.juju.list_applications(model))
        if existing is not None:
            return existing
        try:
            self.juju.deploy_application(
                model,
                ISTIO_CONTROL_PLANE_CHARM,
                application=ISTIO_CONTROL_PLANE_CHARM,
                trust=True,
                channel=ISTIO_CONTROL_PLANE_CHANNEL,
            )
            self.logger.info(f"Deployed '{ISTIO_CONTROL_PLANE_CHARM}' into '{model.uri}'.")
            return ISTIO_CONTROL_PLANE_CHARM
        except Exception as error:
            # Re-check authoritative state instead of pattern-matching backend errors, to
            # tolerate a concurrent worker having already deployed istio-k8s.
            existing = self._find_istio_application(self.juju.list_applications(model))
            if existing is None:
                raise error from None
            return existing

    @staticmethod
    def _find_istio_application(applications: dict[str, JujuApplicationInfo]) -> str | None:
        for application, info in applications.items():
            if info.charm == ISTIO_CONTROL_PLANE_CHARM:
                return application
        return None
