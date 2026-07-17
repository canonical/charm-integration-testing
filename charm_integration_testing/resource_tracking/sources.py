# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Kubernetes sources that collect resource snapshots from a live cluster.

Each source knows how to turn one kind of raw Kubernetes object into
:class:`~resource_tracking.snapshot.ResourceSnapshot` values.  Keeping the
mapping here -- rather than in ``kubernetes_client`` -- lets the Kubernetes
facade stay in the business of returning raw API objects (like ``get_charm_pods``
does) while all snapshot construction lives alongside the collector that consumes
it.  These sources are Kubernetes-specific; other substrates (LXD, OpenStack)
would supply their own collector rather than reusing this interface.  New
Kubernetes resource kinds are supported by adding another source.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from kubernetes import client as k8s_client  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient

from .snapshot import (
    ConfigMapSnapshot,
    DeploymentSnapshot,
    IngressSnapshot,
    NetworkPolicySnapshot,
    PvcSnapshot,
    ResourceSnapshot,
    RoleBindingSnapshot,
    RoleSnapshot,
    SecretSnapshot,
    ServiceAccountSnapshot,
    ServiceSnapshot,
    StatefulSetSnapshot,
)


def _application(labels: Mapping[str, str] | None) -> str:
    """Return the owning application from the standard Kubernetes name label."""
    return (labels or {}).get("app.kubernetes.io/name", "")


def _images(spec: Any) -> str:
    """Return a stable, comma-joined view of a workload's container images."""
    template = spec.template if spec is not None else None
    pod_spec = template.spec if template is not None else None
    containers = pod_spec.containers if pod_spec is not None else None
    if not containers:
        return ""
    return ",".join(sorted(container.image or "" for container in containers))


def _ports(spec: Any) -> str:
    """Return a stable, comma-joined ``port/protocol`` view of a service's ports."""
    ports = spec.ports if spec is not None else None
    if not ports:
        return ""
    return ",".join(sorted(f"{port.port}/{port.protocol or 'TCP'}" for port in ports))


def _keys(data: Mapping[str, str] | None) -> str:
    """Return the sorted, comma-joined keys of a data mapping (values excluded)."""
    return ",".join(sorted((data or {}).keys()))


def _rules(rules: Any) -> str:
    """Return a stable ``verbs:resources`` summary of RBAC policy rules."""
    if not rules:
        return ""
    summaries = [f"{','.join(sorted(rule.verbs or []))}:{','.join(sorted(rule.resources or []))}" for rule in rules]
    return ";".join(sorted(summaries))


def _subjects(subjects: Any) -> str:
    """Return a stable ``kind/name`` summary of RoleBinding subjects."""
    if not subjects:
        return ""
    return ",".join(sorted(f"{subject.kind}/{subject.name}" for subject in subjects))


def _hosts(spec: Any) -> str:
    """Return a stable, comma-joined view of an ingress's declared hosts."""
    rules = spec.rules if spec is not None else None
    if not rules:
        return ""
    return ",".join(sorted(rule.host or "" for rule in rules))


class KubernetesResourceSource(Protocol):
    """Collects snapshots of a single Kubernetes resource kind for one model."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        """Return snapshots for this resource kind in ``model``'s namespace.

        Raises:
            kubernetes.client.ApiException: If the cluster query fails.
        """


class PvcSource:
    """Collects :class:`PvcSnapshot` values from a model's namespace."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        pvcs = kubernetes_client.list_model_pvcs(model=model)
        snapshots: list[ResourceSnapshot] = []
        for pvc in pvcs:
            # ``spec``, ``spec.resources`` and ``status`` are optional on the raw
            # Kubernetes objects and may be ``None``; guard each hop so a partially
            # populated PVC does not abort collection for the whole model.
            spec = pvc.spec
            status = pvc.status
            requests = spec.resources.requests if spec is not None and spec.resources is not None else None
            snapshots.append(
                PvcSnapshot(
                    name=pvc.metadata.name,
                    namespace=model,
                    storage_class=(spec.storage_class_name if spec is not None else None) or "",
                    requested_storage=(requests or {}).get("storage", ""),
                    phase=(status.phase if status is not None else None) or "",
                    application=(pvc.metadata.labels or {}).get("app.kubernetes.io/name", ""),
                )
            )
        return snapshots


class StatefulSetSource:
    """Collects :class:`StatefulSetSnapshot` values from a model's namespace."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        stateful_sets = kubernetes_client.backend.apps_v1_api.list_namespaced_stateful_set(model)
        snapshots: list[ResourceSnapshot] = []
        for stateful_set in stateful_sets.items:
            spec = stateful_set.spec
            replicas = spec.replicas if spec is not None else None
            snapshots.append(
                StatefulSetSnapshot(
                    name=stateful_set.metadata.name,
                    namespace=model,
                    replicas="" if replicas is None else str(replicas),
                    image=_images(spec),
                    application=_application(stateful_set.metadata.labels),
                )
            )
        return snapshots


class DeploymentSource:
    """Collects :class:`DeploymentSnapshot` values from a model's namespace."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        deployments = kubernetes_client.backend.apps_v1_api.list_namespaced_deployment(model)
        snapshots: list[ResourceSnapshot] = []
        for deployment in deployments.items:
            spec = deployment.spec
            replicas = spec.replicas if spec is not None else None
            snapshots.append(
                DeploymentSnapshot(
                    name=deployment.metadata.name,
                    namespace=model,
                    replicas="" if replicas is None else str(replicas),
                    image=_images(spec),
                    application=_application(deployment.metadata.labels),
                )
            )
        return snapshots


class ServiceSource:
    """Collects :class:`ServiceSnapshot` values from a model's namespace."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        services = kubernetes_client.backend.core_v1_api.list_namespaced_service(model)
        snapshots: list[ResourceSnapshot] = []
        for service in services.items:
            spec = service.spec
            snapshots.append(
                ServiceSnapshot(
                    name=service.metadata.name,
                    namespace=model,
                    service_type=(spec.type if spec is not None else None) or "",
                    cluster_ip=(spec.cluster_ip if spec is not None else None) or "",
                    ports=_ports(spec),
                    application=_application(service.metadata.labels),
                )
            )
        return snapshots


class ConfigMapSource:
    """Collects :class:`ConfigMapSnapshot` values from a model's namespace."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        config_maps = kubernetes_client.backend.core_v1_api.list_namespaced_config_map(model)
        snapshots: list[ResourceSnapshot] = []
        for config_map in config_maps.items:
            snapshots.append(
                ConfigMapSnapshot(
                    name=config_map.metadata.name,
                    namespace=model,
                    data_keys=_keys(config_map.data),
                    application=_application(config_map.metadata.labels),
                )
            )
        return snapshots


class SecretSource:
    """Collects :class:`SecretSnapshot` values from a model's namespace."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        secrets = kubernetes_client.backend.core_v1_api.list_namespaced_secret(model)
        snapshots: list[ResourceSnapshot] = []
        for secret in secrets.items:
            snapshots.append(
                SecretSnapshot(
                    name=secret.metadata.name,
                    namespace=model,
                    secret_type=secret.type or "",
                    data_keys=_keys(secret.data),
                    application=_application(secret.metadata.labels),
                )
            )
        return snapshots


class ServiceAccountSource:
    """Collects :class:`ServiceAccountSnapshot` values from a model's namespace."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        service_accounts = kubernetes_client.backend.core_v1_api.list_namespaced_service_account(model)
        snapshots: list[ResourceSnapshot] = []
        for service_account in service_accounts.items:
            snapshots.append(
                ServiceAccountSnapshot(
                    name=service_account.metadata.name,
                    namespace=model,
                    application=_application(service_account.metadata.labels),
                )
            )
        return snapshots


class RoleSource:
    """Collects :class:`RoleSnapshot` values from a model's namespace.

    The RBAC API group is not instantiated on :class:`KubernetesBackend`, so it
    is constructed here from the shared ``api_client`` to keep this addition
    contained to the resource-tracking layer.
    """

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        rbac_api = k8s_client.RbacAuthorizationV1Api(kubernetes_client.backend.api_client)
        roles = rbac_api.list_namespaced_role(model)
        snapshots: list[ResourceSnapshot] = []
        for role in roles.items:
            snapshots.append(
                RoleSnapshot(
                    name=role.metadata.name,
                    namespace=model,
                    rules=_rules(role.rules),
                    application=_application(role.metadata.labels),
                )
            )
        return snapshots


class RoleBindingSource:
    """Collects :class:`RoleBindingSnapshot` values from a model's namespace."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        rbac_api = k8s_client.RbacAuthorizationV1Api(kubernetes_client.backend.api_client)
        role_bindings = rbac_api.list_namespaced_role_binding(model)
        snapshots: list[ResourceSnapshot] = []
        for role_binding in role_bindings.items:
            role_ref = role_binding.role_ref
            snapshots.append(
                RoleBindingSnapshot(
                    name=role_binding.metadata.name,
                    namespace=model,
                    role_ref=f"{role_ref.kind}/{role_ref.name}" if role_ref is not None else "",
                    subjects=_subjects(role_binding.subjects),
                    application=_application(role_binding.metadata.labels),
                )
            )
        return snapshots


class NetworkPolicySource:
    """Collects :class:`NetworkPolicySnapshot` values from a model's namespace.

    The networking API group is not instantiated on :class:`KubernetesBackend`,
    so it is constructed here from the shared ``api_client``.
    """

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        networking_api = k8s_client.NetworkingV1Api(kubernetes_client.backend.api_client)
        policies = networking_api.list_namespaced_network_policy(model)
        snapshots: list[ResourceSnapshot] = []
        for policy in policies.items:
            spec = policy.spec
            policy_types = spec.policy_types if spec is not None else None
            snapshots.append(
                NetworkPolicySnapshot(
                    name=policy.metadata.name,
                    namespace=model,
                    policy_types=",".join(sorted(policy_types or [])),
                    application=_application(policy.metadata.labels),
                )
            )
        return snapshots


class IngressSource:
    """Collects :class:`IngressSnapshot` values from a model's namespace."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        networking_api = k8s_client.NetworkingV1Api(kubernetes_client.backend.api_client)
        ingresses = networking_api.list_namespaced_ingress(model)
        snapshots: list[ResourceSnapshot] = []
        for ingress in ingresses.items:
            spec = ingress.spec
            snapshots.append(
                IngressSnapshot(
                    name=ingress.metadata.name,
                    namespace=model,
                    ingress_class=(spec.ingress_class_name if spec is not None else None) or "",
                    hosts=_hosts(spec),
                    application=_application(ingress.metadata.labels),
                )
            )
        return snapshots
