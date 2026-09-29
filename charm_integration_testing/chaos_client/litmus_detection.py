# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from kubernetes_client import KubernetesBackend

LITMUS_CRDS = (
    "chaosengines.litmuschaos.io",
    "chaosexperiments.litmuschaos.io",
    "chaosresults.litmuschaos.io",
)
OPERATOR_NAMESPACE = "litmus-system"
OPERATOR_DEPLOYMENT = "litmus"


def litmus_is_available(backend: KubernetesBackend) -> bool:
    """Check the shared Litmus CRDs and operator readiness without caching results."""
    # Check every CRD so an absent one does not hide an API error for another.
    present = [backend.crd_exists(name) for name in LITMUS_CRDS]
    return all(present) and backend.deployment_is_ready(OPERATOR_NAMESPACE, OPERATOR_DEPLOYMENT)
