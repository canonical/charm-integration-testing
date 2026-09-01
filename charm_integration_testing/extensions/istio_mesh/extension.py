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

DEFAULT_ISTIO_DEPLOY_TIMEOUT = timedelta(minutes=15)


class IstioMeshExtension(JujuExtension):
    """Deploys istio-k8s into a model when a mesh-dependent charm is present but has no
    istio-k8s of its own to reconcile against.

    istio-beacon-k8s and istio-ingress-k8s each need a reconciling istio-k8s control plane
    in the *same model* to reach "active", with no Juju relation to express the dependency.
    Checked via Juju application state rather than Kubernetes cluster state: the Gateway API
    CRDs istio-k8s installs are cluster-scoped, so their presence says nothing about whether
    *this* model has a working control plane -- a model on a cluster where istio-k8s happens
    to be deployed elsewhere still hangs the same way (confirmed live: istio-ingress-k8s's
    Gateway stayed unprogrammed even with the CRD already present cluster-wide from another
    model's istio-k8s). Deployment is idempotent: an already-deployed istio-k8s application
    in this model is treated as success, and is only ever attempted once even if multiple
    dependents are present.

    Design trade-off vs. #922 (ClusterAddonExtension, https://github.com/canonical/charm-integration-testing/pull/922):
    #922 introduces a generic, declarative "cluster addon" mechanism (a new
    ``cluster_addons``/``addon_scope`` override schema, a shared cluster-wide addon model,
    and matching bundle-builder/override changes) capable of expressing arbitrary
    addon-charm-to-dependent-charm relationships. This extension instead hardcodes exactly
    the two known dependents and one control-plane charm as constants above, at roughly a
    third of #922's diff size and half its file count (see PR #936's description for the
    exact comparison). That keeps this extension cheap and easy to read for the istio-mesh
    case specifically, but the trade-off is that it does not generalize: a third,
    structurally different addon-dependency pattern (e.g. a future config-gated case like
    gateway-api-integrator) would need either a similar one-off extension or a rewrite
    toward #922's generic mechanism, rather than a one-line addition here.
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

        existing = self._find_istio_application(applications)
        if existing is not None:
            self.juju.wait_application_settled(model, existing, timeout=DEFAULT_ISTIO_DEPLOY_TIMEOUT)
            return

        self.logger.info(
            f"{dependents} present in '{model.uri}' with no '{ISTIO_CONTROL_PLANE_CHARM}' of its own; "
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
