# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the per-kind Kubernetes resource sources and their snapshots.

Each source turns raw Kubernetes API objects into immutable snapshots.  The raw
objects are faked with :class:`~types.SimpleNamespace` so the mapping can be
exercised without a live cluster.  Sources that reach for an API group not held
on :class:`KubernetesBackend` (RBAC, networking) build it from
``backend.api_client``; those constructors are monkeypatched to return fakes.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes import client as k8s_client  # type: ignore[import-untyped]
from resource_tracking.snapshot import (
    ConfigMapSnapshot,
    DeploymentSnapshot,
    IngressSnapshot,
    NetworkPolicySnapshot,
    RoleBindingSnapshot,
    RoleSnapshot,
    SecretSnapshot,
    ServiceAccountSnapshot,
    ServiceSnapshot,
    StatefulSetSnapshot,
)
from resource_tracking.sources import (
    DEFAULT_KUBERNETES_SOURCES,
    ConfigMapSource,
    DeploymentSource,
    IngressSource,
    NetworkPolicySource,
    PvcSource,
    RoleBindingSource,
    RoleSource,
    SecretSource,
    ServiceAccountSource,
    ServiceSource,
    StatefulSetSource,
)

MODEL = "test-model"


def _meta(name: str, labels: dict[str, str] | None = None, generate_name: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, labels=labels, generate_name=generate_name)


def _listing(items: list[Any]) -> SimpleNamespace:
    """Mimic a V1*List object, which exposes an ``items`` attribute."""
    return SimpleNamespace(items=items)


class _FakeAppsApi:
    """Stand-in for AppsV1Api exposing only the list calls the sources use."""

    def __init__(
        self,
        stateful_sets: list[Any] | None = None,
        deployments: list[Any] | None = None,
    ) -> None:
        self._stateful_sets = stateful_sets or []
        self._deployments = deployments or []
        self.requested_model: str | None = None

    def list_namespaced_stateful_set(self, model: str) -> SimpleNamespace:
        self.requested_model = model
        return _listing(self._stateful_sets)

    def list_namespaced_deployment(self, model: str) -> SimpleNamespace:
        self.requested_model = model
        return _listing(self._deployments)


class _FakeCoreApi:
    """Stand-in for CoreV1Api exposing only the list calls the sources use."""

    def __init__(
        self,
        services: list[Any] | None = None,
        config_maps: list[Any] | None = None,
        secrets: list[Any] | None = None,
        service_accounts: list[Any] | None = None,
    ) -> None:
        self._services = services or []
        self._config_maps = config_maps or []
        self._secrets = secrets or []
        self._service_accounts = service_accounts or []

    def list_namespaced_service(self, model: str) -> SimpleNamespace:
        return _listing(self._services)

    def list_namespaced_config_map(self, model: str) -> SimpleNamespace:
        return _listing(self._config_maps)

    def list_namespaced_secret(self, model: str) -> SimpleNamespace:
        return _listing(self._secrets)

    def list_namespaced_service_account(self, model: str) -> SimpleNamespace:
        return _listing(self._service_accounts)


class _FakeRbacApi:
    """Stand-in for RbacAuthorizationV1Api."""

    def __init__(self, roles: list[Any] | None = None, role_bindings: list[Any] | None = None) -> None:
        self._roles = roles or []
        self._role_bindings = role_bindings or []

    def list_namespaced_role(self, model: str) -> SimpleNamespace:
        return _listing(self._roles)

    def list_namespaced_role_binding(self, model: str) -> SimpleNamespace:
        return _listing(self._role_bindings)


class _FakeNetworkingApi:
    """Stand-in for NetworkingV1Api."""

    def __init__(self, policies: list[Any] | None = None, ingresses: list[Any] | None = None) -> None:
        self._policies = policies or []
        self._ingresses = ingresses or []

    def list_namespaced_network_policy(self, model: str) -> SimpleNamespace:
        return _listing(self._policies)

    def list_namespaced_ingress(self, model: str) -> SimpleNamespace:
        return _listing(self._ingresses)


def _client(apps: Any = None, core: Any = None) -> SimpleNamespace:
    """Build a fake KubernetesClient exposing the ``backend`` the sources reach for."""
    return SimpleNamespace(backend=SimpleNamespace(apps_v1_api=apps, core_v1_api=core, api_client=object()))


def _patch_rbac(monkeypatch: pytest.MonkeyPatch, api: _FakeRbacApi) -> None:
    monkeypatch.setattr(k8s_client, "RbacAuthorizationV1Api", lambda _api_client: api)


def _patch_networking(monkeypatch: pytest.MonkeyPatch, api: _FakeNetworkingApi) -> None:
    monkeypatch.setattr(k8s_client, "NetworkingV1Api", lambda _api_client: api)


class TestStatefulSetSource:
    def test_maps_replicas_image_and_application(self) -> None:
        # GIVEN a raw StatefulSet with two containers and an owning-application label
        raw = SimpleNamespace(
            metadata=_meta("postgresql", labels={"app.kubernetes.io/name": "postgresql"}),
            spec=SimpleNamespace(
                replicas=3,
                template=SimpleNamespace(
                    spec=SimpleNamespace(containers=[SimpleNamespace(image="b:2"), SimpleNamespace(image="a:1")])
                ),
            ),
        )
        client = _client(apps=_FakeAppsApi(stateful_sets=[raw]))

        # WHEN the source collects snapshots for the model
        snapshots = StatefulSetSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN replicas is stringified, images are sorted+joined, and the model is queried
        assert snapshots == [
            StatefulSetSnapshot(
                name="postgresql",
                namespace=MODEL,
                replicas="3",
                image="a:1,b:2",
                application="postgresql",
            )
        ]
        assert client.backend.apps_v1_api.requested_model == MODEL

    def test_none_spec_defaults_to_empty_without_raising(self) -> None:
        # GIVEN a raw StatefulSet whose spec subtree is entirely None
        raw = SimpleNamespace(metadata=_meta("sts", labels=None), spec=None)
        client = _client(apps=_FakeAppsApi(stateful_sets=[raw]))

        # WHEN the source collects snapshots
        snapshots = StatefulSetSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN replicas and image default to empty and application is empty
        assert snapshots == [StatefulSetSnapshot(name="sts", namespace=MODEL, replicas="", image="")]


class TestDeploymentSource:
    def test_maps_replicas_image_and_application(self) -> None:
        # GIVEN a raw Deployment with one container
        raw = SimpleNamespace(
            metadata=_meta("dex", labels={"app.kubernetes.io/name": "dex"}),
            spec=SimpleNamespace(
                replicas=1,
                template=SimpleNamespace(spec=SimpleNamespace(containers=[SimpleNamespace(image="dex:1")])),
            ),
        )
        client = _client(apps=_FakeAppsApi(deployments=[raw]))

        # WHEN the source collects snapshots
        snapshots = DeploymentSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the fields are mapped onto a DeploymentSnapshot
        assert snapshots == [
            DeploymentSnapshot(name="dex", namespace=MODEL, replicas="1", image="dex:1", application="dex")
        ]


class TestServiceSource:
    def test_maps_type_ports_and_excludes_cluster_ip_from_identity(self) -> None:
        # GIVEN a raw Service with two ports
        raw = SimpleNamespace(
            metadata=_meta("postgresql", labels={"app.kubernetes.io/name": "postgresql"}),
            spec=SimpleNamespace(
                type="ClusterIP",
                cluster_ip="10.1.2.3",
                ports=[SimpleNamespace(port=5432, protocol="TCP"), SimpleNamespace(port=8008, protocol=None)],
            ),
        )
        client = _client(core=_FakeCoreApi(services=[raw]))

        # WHEN the source collects snapshots
        snapshots = ServiceSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN ports are sorted+joined (protocol defaulting to TCP) and cluster_ip is captured
        snapshot = snapshots[0]
        assert snapshot == ServiceSnapshot(
            name="postgresql",
            namespace=MODEL,
            service_type="ClusterIP",
            cluster_ip="10.1.2.3",
            ports="5432/TCP,8008/TCP",
            application="postgresql",
        )
        # AND the volatile cluster_ip is excluded from identity
        assert "10.1.2.3" not in snapshot.identity

    def test_none_spec_defaults_to_empty(self) -> None:
        # GIVEN a raw Service without a spec
        raw = SimpleNamespace(metadata=_meta("svc", labels=None), spec=None)
        client = _client(core=_FakeCoreApi(services=[raw]))

        # WHEN the source collects snapshots
        snapshots = ServiceSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the optional fields default to empty
        assert snapshots == [ServiceSnapshot(name="svc", namespace=MODEL, service_type="", cluster_ip="", ports="")]


class TestConfigMapSource:
    def test_records_sorted_keys_and_excludes_values_from_identity(self) -> None:
        # GIVEN a raw ConfigMap with data values
        raw = SimpleNamespace(
            metadata=_meta("kube-root-ca.crt", labels=None),
            data={"b.conf": "secret-value", "a.conf": "another"},
        )
        client = _client(core=_FakeCoreApi(config_maps=[raw]))

        # WHEN the source collects snapshots
        snapshots = ConfigMapSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN only the sorted keys are recorded, never the values
        snapshot = snapshots[0]
        assert snapshot == ConfigMapSnapshot(name="kube-root-ca.crt", namespace=MODEL, data_keys="a.conf,b.conf")
        assert "secret-value" not in "".join(snapshot.identity)

    def test_none_data_yields_empty_keys(self) -> None:
        # GIVEN a raw ConfigMap without data
        raw = SimpleNamespace(metadata=_meta("empty", labels=None), data=None)
        client = _client(core=_FakeCoreApi(config_maps=[raw]))

        # WHEN the source collects snapshots
        snapshots = ConfigMapSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN data_keys is empty
        assert snapshots == [ConfigMapSnapshot(name="empty", namespace=MODEL, data_keys="")]


class TestSecretSource:
    def test_records_type_and_keys_without_values(self) -> None:
        # GIVEN a raw Secret with a type and data
        raw = SimpleNamespace(
            metadata=_meta("postgresql.app", labels={"app.kubernetes.io/name": "postgresql"}),
            type="Opaque",
            data={"password": "cGFzcw==", "username": "dXNlcg=="},
        )
        client = _client(core=_FakeCoreApi(secrets=[raw]))

        # WHEN the source collects snapshots
        snapshots = SecretSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the type and sorted keys are recorded, never the values
        snapshot = snapshots[0]
        assert snapshot == SecretSnapshot(
            name="postgresql.app",
            namespace=MODEL,
            secret_type="Opaque",
            data_keys="password,username",
            application="postgresql",
        )
        assert "cGFzcw==" not in "".join(snapshot.identity)

    def test_none_type_and_data_default_to_empty(self) -> None:
        # GIVEN a raw Secret without a type or data
        raw = SimpleNamespace(metadata=_meta("blank", labels=None), type=None, data=None)
        client = _client(core=_FakeCoreApi(secrets=[raw]))

        # WHEN the source collects snapshots
        snapshots = SecretSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the optional fields default to empty
        assert snapshots == [SecretSnapshot(name="blank", namespace=MODEL, secret_type="", data_keys="")]

    def test_service_account_token_secrets_are_skipped(self) -> None:
        # GIVEN a service-account-token Secret (volatile ``<sa>-token-XXXXX`` name)
        raw = SimpleNamespace(
            metadata=_meta("postgresql-token-ab12c", labels=None),
            type="kubernetes.io/service-account-token",
            data={"ca.crt": "Y2E=", "namespace": "bnM=", "token": "dG9r"},
        )
        client = _client(core=_FakeCoreApi(secrets=[raw]))

        # WHEN the source collects snapshots
        snapshots = SecretSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the volatile-named token secret is not tracked
        assert snapshots == []

    def test_generate_name_secrets_are_skipped(self) -> None:
        # GIVEN a Secret created with generateName (server-appended random suffix)
        raw = SimpleNamespace(
            metadata=_meta("ephemeral-x9k2p", labels=None, generate_name="ephemeral-"),
            type="Opaque",
            data={"token": "dG9r"},
        )
        client = _client(core=_FakeCoreApi(secrets=[raw]))

        # WHEN the source collects snapshots
        snapshots = SecretSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the generated-name secret is not tracked
        assert snapshots == []

    def test_stable_secret_is_tracked_alongside_skipped_ones(self) -> None:
        # GIVEN a stable Opaque secret and a volatile token secret in the same model
        stable = SimpleNamespace(
            metadata=_meta("postgresql.app", labels={"app.kubernetes.io/name": "postgresql"}),
            type="Opaque",
            data={"password": "cGFzcw=="},
        )
        token = SimpleNamespace(
            metadata=_meta("postgresql-token-ab12c", labels=None),
            type="kubernetes.io/service-account-token",
            data={"token": "dG9r"},
        )
        client = _client(core=_FakeCoreApi(secrets=[stable, token]))

        # WHEN the source collects snapshots
        snapshots = SecretSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN only the stable secret is tracked
        assert snapshots == [
            SecretSnapshot(
                name="postgresql.app",
                namespace=MODEL,
                secret_type="Opaque",
                data_keys="password",
                application="postgresql",
            )
        ]


class TestServiceAccountSource:
    def test_maps_name_and_application(self) -> None:
        # GIVEN a raw ServiceAccount labelled with its owning application
        raw = SimpleNamespace(metadata=_meta("postgresql", labels={"app.kubernetes.io/name": "postgresql"}))
        client = _client(core=_FakeCoreApi(service_accounts=[raw]))

        # WHEN the source collects snapshots
        snapshots = ServiceAccountSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the snapshot records the name, namespace and application
        assert snapshots == [ServiceAccountSnapshot(name="postgresql", namespace=MODEL, application="postgresql")]


class TestRoleSource:
    def test_summarises_rules_as_sorted_verbs_and_resources(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a raw Role with a policy rule
        raw = SimpleNamespace(
            metadata=_meta("postgresql", labels={"app.kubernetes.io/name": "postgresql"}),
            rules=[SimpleNamespace(verbs=["watch", "get"], resources=["pods", "endpoints"])],
        )
        _patch_rbac(monkeypatch, _FakeRbacApi(roles=[raw]))
        client = _client()

        # WHEN the source collects snapshots
        snapshots = RoleSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN verbs and resources are sorted within a stable ``verbs:resources`` summary
        assert snapshots == [
            RoleSnapshot(
                name="postgresql",
                namespace=MODEL,
                rules="get,watch:endpoints,pods",
                application="postgresql",
            )
        ]

    def test_none_rules_yield_empty_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a raw Role without rules
        raw = SimpleNamespace(metadata=_meta("empty", labels=None), rules=None)
        _patch_rbac(monkeypatch, _FakeRbacApi(roles=[raw]))
        client = _client()

        # WHEN the source collects snapshots
        snapshots = RoleSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the rules summary is empty
        assert snapshots == [RoleSnapshot(name="empty", namespace=MODEL, rules="")]


class TestRoleBindingSource:
    def test_maps_role_ref_and_subjects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a raw RoleBinding with a role ref and two subjects
        raw = SimpleNamespace(
            metadata=_meta("postgresql", labels={"app.kubernetes.io/name": "postgresql"}),
            role_ref=SimpleNamespace(kind="Role", name="postgresql"),
            subjects=[
                SimpleNamespace(kind="ServiceAccount", name="postgresql"),
                SimpleNamespace(kind="Group", name="admins"),
            ],
        )
        _patch_rbac(monkeypatch, _FakeRbacApi(role_bindings=[raw]))
        client = _client()

        # WHEN the source collects snapshots
        snapshots = RoleBindingSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the role ref and sorted subjects are captured
        assert snapshots == [
            RoleBindingSnapshot(
                name="postgresql",
                namespace=MODEL,
                role_ref="Role/postgresql",
                subjects="Group/admins,ServiceAccount/postgresql",
                application="postgresql",
            )
        ]

    def test_none_role_ref_and_subjects_default_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a raw RoleBinding without a role ref or subjects
        raw = SimpleNamespace(metadata=_meta("rb", labels=None), role_ref=None, subjects=None)
        _patch_rbac(monkeypatch, _FakeRbacApi(role_bindings=[raw]))
        client = _client()

        # WHEN the source collects snapshots
        snapshots = RoleBindingSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the optional fields default to empty
        assert snapshots == [RoleBindingSnapshot(name="rb", namespace=MODEL, role_ref="", subjects="")]


class TestNetworkPolicySource:
    def test_records_sorted_policy_types(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a raw NetworkPolicy declaring both policy types
        raw = SimpleNamespace(
            metadata=_meta("postgresql", labels={"app.kubernetes.io/name": "postgresql"}),
            spec=SimpleNamespace(policy_types=["Ingress", "Egress"]),
        )
        _patch_networking(monkeypatch, _FakeNetworkingApi(policies=[raw]))
        client = _client()

        # WHEN the source collects snapshots
        snapshots = NetworkPolicySource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the policy types are sorted+joined
        assert snapshots == [
            NetworkPolicySnapshot(
                name="postgresql",
                namespace=MODEL,
                policy_types="Egress,Ingress",
                application="postgresql",
            )
        ]

    def test_none_spec_yields_empty_policy_types(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a raw NetworkPolicy without a spec
        raw = SimpleNamespace(metadata=_meta("np", labels=None), spec=None)
        _patch_networking(monkeypatch, _FakeNetworkingApi(policies=[raw]))
        client = _client()

        # WHEN the source collects snapshots
        snapshots = NetworkPolicySource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN policy_types is empty
        assert snapshots == [NetworkPolicySnapshot(name="np", namespace=MODEL, policy_types="")]


class TestIngressSource:
    def test_maps_class_and_sorted_hosts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a raw Ingress with two host rules
        raw = SimpleNamespace(
            metadata=_meta("app-ingress", labels={"app.kubernetes.io/name": "app"}),
            spec=SimpleNamespace(
                ingress_class_name="nginx",
                rules=[SimpleNamespace(host="b.example.com"), SimpleNamespace(host="a.example.com")],
            ),
        )
        _patch_networking(monkeypatch, _FakeNetworkingApi(ingresses=[raw]))
        client = _client()

        # WHEN the source collects snapshots
        snapshots = IngressSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the ingress class and sorted hosts are captured
        assert snapshots == [
            IngressSnapshot(
                name="app-ingress",
                namespace=MODEL,
                ingress_class="nginx",
                hosts="a.example.com,b.example.com",
                application="app",
            )
        ]

    def test_none_spec_defaults_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a raw Ingress without a spec
        raw = SimpleNamespace(metadata=_meta("ing", labels=None), spec=None)
        _patch_networking(monkeypatch, _FakeNetworkingApi(ingresses=[raw]))
        client = _client()

        # WHEN the source collects snapshots
        snapshots = IngressSource().collect(client, MODEL)  # type: ignore[arg-type]

        # THEN the optional fields default to empty
        assert snapshots == [IngressSnapshot(name="ing", namespace=MODEL, ingress_class="", hosts="")]


class TestDefaultKubernetesSources:
    def test_covers_every_source_kind(self) -> None:
        # GIVEN the canonical source list used to drive live collection
        # THEN it holds exactly one instance of each implemented source kind
        assert {type(source) for source in DEFAULT_KUBERNETES_SOURCES} == {
            PvcSource,
            StatefulSetSource,
            DeploymentSource,
            ServiceSource,
            ConfigMapSource,
            SecretSource,
            ServiceAccountSource,
            RoleSource,
            RoleBindingSource,
            NetworkPolicySource,
            IngressSource,
        }
        assert len(DEFAULT_KUBERNETES_SOURCES) == 11
