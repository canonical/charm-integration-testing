# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Resource snapshot types tracked across scheduler states.

A snapshot is an immutable, hashable view of a single cluster resource.  The
tracker and discrepancy calculator depend only on the :class:`ResourceSnapshot`
structural interface, never on a concrete resource type, so a new resource kind
becomes trackable simply by adding a snapshot type that implements it -- no
change to the tracker, calculator, or report is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Mapping, Protocol, runtime_checkable


@runtime_checkable
class ResourceSnapshot(Protocol):
    """Structural contract a resource snapshot must satisfy to be tracked.

    The members are read-only so that frozen dataclasses (whose fields are
    immutable) and ``ClassVar`` labels both satisfy the contract.
    """

    @property
    def resource_type(self) -> str:
        """Short label used in report keys, e.g. ``pvc``."""

    @property
    def name(self) -> str:
        """Human-readable resource name used in reports."""

    @property
    def application(self) -> str:
        """Owning Juju application, used to apply per-charm tracking overrides.

        Empty when the resource cannot be attributed to an application.
        """

    @property
    def identity(self) -> tuple[str, ...]:
        """Stable identity used to diff snapshots across repeated state visits."""

    def report_attributes(self) -> Mapping[str, str]:
        """Resource-specific ``key=value`` attributes for the report line."""


@dataclass(frozen=True)
class PvcSnapshot:
    """Immutable point-in-time view of a single PersistentVolumeClaim.

    Frozen and hashable so snapshots can be collected into sets and diffed
    across repeated visits to the same scheduler state.
    """

    name: str
    namespace: str
    storage_class: str
    requested_storage: str
    phase: str
    application: str = ""

    resource_type: ClassVar[str] = "pvc"

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """Stable identity used to diff snapshots across state visits.

        Excludes the volatile ``phase`` field so that a claim which is merely
        transitioning (e.g. ``Pending`` vs ``Bound``) is not reported as a
        different resource.
        """
        return (self.namespace, self.name, self.storage_class, self.requested_storage)

    def report_attributes(self) -> dict[str, str]:
        """Resource-specific ``key=value`` attributes for the report line."""
        return {
            "storage_class": self.storage_class,
            "requested_storage": self.requested_storage,
        }


@dataclass(frozen=True)
class StatefulSetSnapshot:
    """Immutable point-in-time view of a single StatefulSet.

    StatefulSets are how Juju runs sidecar k8s charms (one per application), so
    the identity carries the declared ``replicas`` and container ``image`` -- the
    spec fields that describe the intended workload -- while excluding the
    volatile ``status`` (ready/updated replica counts) that merely reflects
    rollout progress.
    """

    name: str
    namespace: str
    replicas: str
    image: str
    application: str = ""

    resource_type: ClassVar[str] = "statefulset"

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.namespace, self.name, self.replicas, self.image)

    def report_attributes(self) -> dict[str, str]:
        return {"replicas": self.replicas, "image": self.image}


@dataclass(frozen=True)
class DeploymentSnapshot:
    """Immutable point-in-time view of a single Deployment.

    Mirrors :class:`StatefulSetSnapshot`: the declared ``replicas`` and container
    ``image`` form the identity, while transient rollout ``status`` is excluded.
    """

    name: str
    namespace: str
    replicas: str
    image: str
    application: str = ""

    resource_type: ClassVar[str] = "deployment"

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.namespace, self.name, self.replicas, self.image)

    def report_attributes(self) -> dict[str, str]:
        return {"replicas": self.replicas, "image": self.image}


@dataclass(frozen=True)
class ServiceSnapshot:
    """Immutable point-in-time view of a single Service.

    The identity excludes ``cluster_ip`` because it is reassigned when a service
    is recreated; the stable ``service_type`` and ``ports`` describe the service
    contract that callers depend on.
    """

    name: str
    namespace: str
    service_type: str
    cluster_ip: str
    ports: str
    application: str = ""

    resource_type: ClassVar[str] = "service"

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.namespace, self.name, self.service_type, self.ports)

    def report_attributes(self) -> dict[str, str]:
        return {"type": self.service_type, "ports": self.ports}


@dataclass(frozen=True)
class ConfigMapSnapshot:
    """Immutable point-in-time view of a single ConfigMap.

    Only the presence and the set of ``data_keys`` form the identity; the values
    are excluded because configuration content legitimately changes between
    scheduler states.
    """

    name: str
    namespace: str
    data_keys: str
    application: str = ""

    resource_type: ClassVar[str] = "configmap"

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

    def report_attributes(self) -> dict[str, str]:
        return {"data_keys": self.data_keys}


@dataclass(frozen=True)
class SecretSnapshot:
    """Immutable point-in-time view of a single Secret.

    The identity carries the ``secret_type`` but never the secret values, which
    are excluded so rotation does not read as drift.
    """

    name: str
    namespace: str
    secret_type: str
    data_keys: str
    application: str = ""

    resource_type: ClassVar[str] = "secret"

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.namespace, self.name, self.secret_type)

    def report_attributes(self) -> dict[str, str]:
        return {"type": self.secret_type, "data_keys": self.data_keys}


@dataclass(frozen=True)
class ServiceAccountSnapshot:
    """Immutable point-in-time view of a single ServiceAccount."""

    name: str
    namespace: str
    application: str = ""

    resource_type: ClassVar[str] = "serviceaccount"

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

    def report_attributes(self) -> dict[str, str]:
        return {}


@dataclass(frozen=True)
class RoleSnapshot:
    """Immutable point-in-time view of a single RBAC Role."""

    name: str
    namespace: str
    rules: str
    application: str = ""

    resource_type: ClassVar[str] = "role"

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

    def report_attributes(self) -> dict[str, str]:
        return {"rules": self.rules}


@dataclass(frozen=True)
class RoleBindingSnapshot:
    """Immutable point-in-time view of a single RBAC RoleBinding."""

    name: str
    namespace: str
    role_ref: str
    subjects: str
    application: str = ""

    resource_type: ClassVar[str] = "rolebinding"

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.namespace, self.name, self.role_ref)

    def report_attributes(self) -> dict[str, str]:
        return {"role_ref": self.role_ref, "subjects": self.subjects}


@dataclass(frozen=True)
class NetworkPolicySnapshot:
    """Immutable point-in-time view of a single NetworkPolicy."""

    name: str
    namespace: str
    policy_types: str
    application: str = ""

    resource_type: ClassVar[str] = "networkpolicy"

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

    def report_attributes(self) -> dict[str, str]:
        return {"policy_types": self.policy_types}


@dataclass(frozen=True)
class IngressSnapshot:
    """Immutable point-in-time view of a single Ingress."""

    name: str
    namespace: str
    ingress_class: str
    hosts: str
    application: str = ""

    resource_type: ClassVar[str] = "ingress"

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

    def report_attributes(self) -> dict[str, str]:
        return {"ingress_class": self.ingress_class, "hosts": self.hosts}
