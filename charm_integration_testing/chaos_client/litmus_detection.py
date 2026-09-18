# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from kubernetes_client import KubernetesBackend

CHAOSENGINE_CRD = "chaosengines.litmuschaos.io"
CHAOS_OPERATOR_DEPLOYMENT = "chaos-operator-ce"


def litmus_is_available(backend: KubernetesBackend, namespace: str) -> bool:
    """Check the Litmus CRD and operator readiness in the execution namespace.

    Results are not cached and API errors propagate. ChaosCenter connectivity
    and experiment execution are not checked.
    """
    return backend.crd_exists(CHAOSENGINE_CRD) and backend.deployment_is_ready(namespace, CHAOS_OPERATOR_DEPLOYMENT)
