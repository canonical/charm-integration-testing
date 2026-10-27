# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import yaml
from juju import JujuApplicationInfo, JujuBackend, JujuExtension, JujuModelHandle

from bundle_builder_x import CharmhubClient
from bundle_builder_x import JujuVersion as BundleJujuVersion

# Charms that need a reconciling istio-k8s control plane to reach "active", with no Juju
# relation to express the dependency.
ISTIO_MESH_DEPENDENT_CHARMS = frozenset({"istio-beacon-k8s", "istio-ingress-k8s"})
ISTIO_CONTROL_PLANE_CHARM = "istio-k8s"


class IstioMeshExtension(JujuExtension):
    """Deploys istio-k8s into a model when a mesh-dependent charm is present but has no
    istio-k8s of its own to reconcile against.

    For bundles without an explicit control plane, ``pre_deploy()`` requests its deployment
    before deploying dependent applications, without waiting for readiness.

    istio-beacon-k8s and istio-ingress-k8s each need a reconciling istio-k8s control plane
    in the *same model* to reach "active", with no Juju relation to express the dependency.
    Dependents are identified from the bundle and existing control planes from Juju application state.
    The Gateway API CRDs istio-k8s installs are cluster-scoped, so their presence says nothing about whether
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

    def __init__(
        self,
        juju: JujuBackend,
        logger: logging.Logger,
        charmhub_client: CharmhubClient,
    ) -> None:
        self.juju = juju
        self.logger = logger
        self.charmhub_client = charmhub_client

    def pre_deploy(self, model: JujuModelHandle, bundle: Path) -> None:
        # Preserve charm names across documents that only add options or offers.
        charms: dict[str, str] = {}
        for document in yaml.safe_load_all(bundle.read_text(encoding="utf-8")):
            if not isinstance(document, dict):
                continue
            applications = document.get("applications")
            if not isinstance(applications, dict):
                continue
            for name, app in applications.items():
                if app is None:
                    charms.pop(name, None)
                elif isinstance(app, dict) and isinstance(app.get("charm"), str):
                    charms[name] = app["charm"].removeprefix("ch:")
        if not ISTIO_MESH_DEPENDENT_CHARMS.intersection(charms.values()):
            return
        # Let the bundle deploy its explicit control plane with its own settings.
        if ISTIO_CONTROL_PLANE_CHARM in charms.values() or not self.juju.is_k8s_model(model):
            return
        self._ensure_istio_deployed(model)

    def _ensure_istio_deployed(self, model: JujuModelHandle) -> str:
        existing = self._find_istio_application(self.juju.list_applications(model))
        if existing is not None:
            return existing
        charm = self.charmhub_client.charm_from_store(
            charm_name=ISTIO_CONTROL_PLANE_CHARM,
            ubuntu_arch=self.juju.model_architecture(model),
            platform="kubernetes",
            juju_version=BundleJujuVersion.parse(str(self.juju.version(model))),
        )
        try:
            self.juju.deploy_application(
                model,
                ISTIO_CONTROL_PLANE_CHARM,
                application=ISTIO_CONTROL_PLANE_CHARM,
                trust=True,
                channel=str(charm.channel),
                revision=charm.revision,
                base=f"ubuntu@{charm.ubuntu_version}",
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
