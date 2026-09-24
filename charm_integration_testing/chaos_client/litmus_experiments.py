# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import Literal

LitmusExperiment = Literal["pod-cpu-hog", "pod-memory-hog"]

_VERSION = "3.31.0"
_IMAGE = f"litmuschaos.docker.scarf.sh/litmuschaos/go-runner:{_VERSION}"
_GROUP = "litmuschaos.io"


def experiment_manifest(namespace: str, name: str, experiment: LitmusExperiment) -> dict[str, object]:
    """Build a CPU or memory experiment definition."""
    if experiment == "pod-cpu-hog":
        settings = {"CPU_CORES": "1", "CPU_LOAD": "100"}
    elif experiment == "pod-memory-hog":
        settings = {"MEMORY_CONSUMPTION": "500", "NUMBER_OF_WORKERS": "1"}
    else:
        raise ValueError(f"Unsupported Litmus experiment: {experiment}")

    settings.update(
        {
            "TOTAL_CHAOS_DURATION": "60",
            "LIB_IMAGE": _IMAGE,
            "CONTAINER_RUNTIME": "containerd",
            "SOCKET_PATH": "/run/containerd/containerd.sock",
            "DEFAULT_HEALTH_CHECK": "false",
            "SEQUENCE": "parallel",
        }
    )
    return {
        "apiVersion": f"{_GROUP}/v1alpha1",
        "kind": "ChaosExperiment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "definition": {
                "scope": "Namespaced",
                "permissions": _permissions(),
                "image": _IMAGE,
                "imagePullPolicy": "Always",
                "command": ["/bin/bash"],
                "args": ["-c", f"./experiments -name {experiment}"],
                "env": [{"name": name, "value": value} for name, value in settings.items()],
                "labels": {
                    "name": name,
                    "app.kubernetes.io/part-of": "litmus",
                    "app.kubernetes.io/component": "experiment-job",
                    "app.kubernetes.io/runtime-api-usage": "true",
                    "app.kubernetes.io/version": _VERSION,
                },
            }
        },
    }


def service_account_manifest(namespace: str, name: str) -> dict[str, object]:
    """Build the experiment service account."""
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": name, "namespace": namespace},
    }


def role_manifest(namespace: str, name: str) -> dict[str, object]:
    """Build namespace permissions for the experiment runner."""
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": name, "namespace": namespace},
        "rules": _permissions(),
    }


def role_binding_manifest(namespace: str, name: str) -> dict[str, object]:
    """Bind the role to the service account with the same name."""
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": namespace},
        "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": namespace}],
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
    }


def _permissions() -> list[dict[str, object]]:
    # Match the permissions declared by the upstream 3.31.0 stress experiments.
    return [
        {
            "apiGroups": [""],
            "resources": ["pods"],
            "verbs": ["create", "delete", "get", "list", "patch", "update", "deletecollection"],
        },
        {
            "apiGroups": [""],
            "resources": ["events"],
            "verbs": ["create", "get", "list", "patch", "update"],
        },
        {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "list"]},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["get", "list", "create"]},
        {
            "apiGroups": ["apps"],
            "resources": ["deployments", "statefulsets", "replicasets", "daemonsets"],
            "verbs": ["list", "get"],
        },
        {"apiGroups": ["apps.openshift.io"], "resources": ["deploymentconfigs"], "verbs": ["list", "get"]},
        {"apiGroups": [""], "resources": ["replicationcontrollers"], "verbs": ["get", "list"]},
        {"apiGroups": ["argoproj.io"], "resources": ["rollouts"], "verbs": ["list", "get"]},
        {
            "apiGroups": ["batch"],
            "resources": ["jobs"],
            "verbs": ["create", "list", "get", "delete", "deletecollection"],
        },
        {
            "apiGroups": [_GROUP],
            "resources": ["chaosengines", "chaosexperiments", "chaosresults"],
            "verbs": ["create", "list", "get", "patch", "update", "delete"],
        },
    ]
