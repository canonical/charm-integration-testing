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


@dataclass(frozen=True)
class InconsistencyCheck:
    """One resource-specific drift kind a snapshot type knows how to report.

    ``qualifier`` is the drift label surfaced in reports (e.g. ``resized``);
    ``attribute`` is the :meth:`ResourceSnapshot.report_attributes` key whose
    change between two visits of the same logical resource constitutes that
    drift.  Naming the *report attribute* (not the dataclass field) lets the
    recorder render a uniform ``old->new`` diff without knowing any concrete
    snapshot type.
    """

    qualifier: str
    attribute: str


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
        """Stable logical identity: the fields that make this the *same* resource
        across repeated state visits, so missing/extra can be diffed on it.

        This is the resource's ``(namespace, name)`` -- deliberately free of
        mutable spec fields, which are compared through :attr:`inconsistency_checks`
        instead so an in-place change reads as a specific drift qualifier rather
        than a missing/extra pair.
        """

    @property
    def inconsistency_checks(self) -> tuple[InconsistencyCheck, ...]:
        """Resource-specific drift kinds to check when this resource re-appears.

        Empty when only presence (missing/extra) is meaningful for the type.
        """

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
    inconsistency_checks: ClassVar[tuple[InconsistencyCheck, ...]] = (
        InconsistencyCheck(qualifier="resized", attribute="requested_storage"),
        InconsistencyCheck(qualifier="storage_class_changed", attribute="storage_class"),
    )

    @property
    def identity(self) -> tuple[str, str]:
        """Stable logical identity used to diff snapshots across state visits.

        Carries only ``(namespace, name)``; the spec fields (``storage_class``,
        ``requested_storage``) are compared through :attr:`inconsistency_checks`
        so an in-place change reads as a ``resized``/``storage_class_changed``
        qualifier, and the volatile ``phase`` is excluded so a claim that is
        merely transitioning (e.g. ``Pending`` vs ``Bound``) is not reported.
        """
        return (self.namespace, self.name)

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
    inconsistency_checks: ClassVar[tuple[InconsistencyCheck, ...]] = (
        InconsistencyCheck(qualifier="scaled", attribute="replicas"),
        InconsistencyCheck(qualifier="image_changed", attribute="image"),
    )

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

    def report_attributes(self) -> dict[str, str]:
        return {"replicas": self.replicas, "image": self.image}


@dataclass(frozen=True)
class DeploymentSnapshot:
    """Immutable point-in-time view of a single Deployment.

    Mirrors :class:`StatefulSetSnapshot`: the declared ``replicas`` and container
    ``image`` are compared through :attr:`inconsistency_checks`, while transient
    rollout ``status`` is excluded.
    """

    name: str
    namespace: str
    replicas: str
    image: str
    application: str = ""

    resource_type: ClassVar[str] = "deployment"
    inconsistency_checks: ClassVar[tuple[InconsistencyCheck, ...]] = (
        InconsistencyCheck(qualifier="scaled", attribute="replicas"),
        InconsistencyCheck(qualifier="image_changed", attribute="image"),
    )

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

    def report_attributes(self) -> dict[str, str]:
        return {"replicas": self.replicas, "image": self.image}


@dataclass(frozen=True)
class ServiceSnapshot:
    """Immutable point-in-time view of a single Service.

    The identity excludes ``cluster_ip`` because it is reassigned when a service
    is recreated; the stable ``service_type`` and ``ports`` describe the service
    contract that callers depend on and are compared through
    :attr:`inconsistency_checks`.
    """

    name: str
    namespace: str
    service_type: str
    cluster_ip: str
    ports: str
    application: str = ""

    resource_type: ClassVar[str] = "service"
    inconsistency_checks: ClassVar[tuple[InconsistencyCheck, ...]] = (
        InconsistencyCheck(qualifier="type_changed", attribute="type"),
        InconsistencyCheck(qualifier="ports_changed", attribute="ports"),
    )

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

    def report_attributes(self) -> dict[str, str]:
        return {"type": self.service_type, "ports": self.ports}


@dataclass(frozen=True)
class ConfigMapSnapshot:
    """Immutable point-in-time view of a single ConfigMap.

    The identity is ``(namespace, name)``; ``data_keys`` is compared through
    :attr:`inconsistency_checks` so a change to the key set on re-entry into the
    same state reads as a ``keys_changed`` qualifier.
    """

    name: str
    namespace: str
    data_keys: str
    application: str = ""

    resource_type: ClassVar[str] = "configmap"
    inconsistency_checks: ClassVar[tuple[InconsistencyCheck, ...]] = (
        InconsistencyCheck(qualifier="keys_changed", attribute="data_keys"),
    )

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

    def report_attributes(self) -> dict[str, str]:
        return {"data_keys": self.data_keys}


@dataclass(frozen=True)
class SecretSnapshot:
    """Immutable point-in-time view of a single Secret.

    The identity is ``(namespace, name)``; the ``secret_type`` and the ``data_keys``
    (key names only, never values, so rotation does not read as drift) are
    compared through :attr:`inconsistency_checks`.
    """

    name: str
    namespace: str
    secret_type: str
    data_keys: str
    application: str = ""

    resource_type: ClassVar[str] = "secret"
    inconsistency_checks: ClassVar[tuple[InconsistencyCheck, ...]] = (
        InconsistencyCheck(qualifier="type_changed", attribute="type"),
        InconsistencyCheck(qualifier="keys_changed", attribute="data_keys"),
    )

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

    def report_attributes(self) -> dict[str, str]:
        return {"type": self.secret_type, "data_keys": self.data_keys}


@dataclass(frozen=True)
class ServiceAccountSnapshot:
    """Immutable point-in-time view of a single ServiceAccount."""

    name: str
    namespace: str
    application: str = ""

    resource_type: ClassVar[str] = "serviceaccount"
    inconsistency_checks: ClassVar[tuple[InconsistencyCheck, ...]] = ()

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
    inconsistency_checks: ClassVar[tuple[InconsistencyCheck, ...]] = (
        InconsistencyCheck(qualifier="rules_changed", attribute="rules"),
    )

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
    inconsistency_checks: ClassVar[tuple[InconsistencyCheck, ...]] = (
        InconsistencyCheck(qualifier="role_ref_changed", attribute="role_ref"),
        InconsistencyCheck(qualifier="subjects_changed", attribute="subjects"),
    )

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

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
    inconsistency_checks: ClassVar[tuple[InconsistencyCheck, ...]] = (
        InconsistencyCheck(qualifier="policy_types_changed", attribute="policy_types"),
    )

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
    inconsistency_checks: ClassVar[tuple[InconsistencyCheck, ...]] = (
        InconsistencyCheck(qualifier="class_changed", attribute="ingress_class"),
        InconsistencyCheck(qualifier="hosts_changed", attribute="hosts"),
    )

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.name)

    def report_attributes(self) -> dict[str, str]:
        return {"ingress_class": self.ingress_class, "hosts": self.hosts}
