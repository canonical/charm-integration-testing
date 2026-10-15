# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta

import pytest
from extensions.unseal_vault.extensions import GenericUnsealVaultJujuExtension, UnsealVaultK8sJujuExtension
from extensions.unseal_vault.vault_unsealer import VaultUnsealer
from juju import JujuModelHandle
from juju.extension import JujuExtension
from kubernetes_client import KubernetesExtension

from ..shared import NullJujuBackend

TEST_MODEL = JujuModelHandle(controller="test-controller", model="test-model")


class JujuBackendStub(NullJujuBackend):
    """Stub JujuBackend that records wait_for_model_to_exist calls."""

    def __init__(self) -> None:
        self.wait_calls: list[JujuModelHandle] = []
        self.namespace_lookups: list[tuple[str, str]] = []
        self.namespace_not_found = False

    def wait_for_model_to_exist(self, model: JujuModelHandle, timeout: timedelta | None) -> None:
        self.wait_calls.append(model)

    def model_for_namespace(self, cloud: str, namespace: str) -> JujuModelHandle | None:
        self.namespace_lookups.append((cloud, namespace))
        if self.namespace_not_found:
            return None
        return JujuModelHandle(controller=f"{cloud}-controller", model=namespace)


class VaultUnsealerStub(VaultUnsealer):
    """Stub VaultUnsealer that records calls instead of touching Juju/Vault."""

    def __init__(self, juju: JujuBackendStub | None = None) -> None:
        self.calls: list[tuple[JujuModelHandle, bool]] = []
        self.juju = juju or JujuBackendStub()

    def try_init_or_unseal_all_vaults(self, model: JujuModelHandle, authorize_charm: bool = True) -> None:
        self.calls.append((model, authorize_charm))


class TestGenericUnsealVaultJujuExtension:
    def test_post_deploy_initializes_and_authorizes(self) -> None:
        # GIVEN an extension wrapping a stub unsealer
        unsealer = VaultUnsealerStub()
        extension = GenericUnsealVaultJujuExtension(unsealer)

        # WHEN post_deploy is called
        extension.post_deploy(TEST_MODEL)

        # THEN the unsealer is invoked with authorization enabled
        assert unsealer.calls == [(TEST_MODEL, True)]

    def test_post_scale_unseals_without_reauthorizing(self) -> None:
        # GIVEN an extension wrapping a stub unsealer
        unsealer = VaultUnsealerStub()
        extension = GenericUnsealVaultJujuExtension(unsealer)

        # WHEN post_scale is called
        extension.post_scale(TEST_MODEL)

        # THEN the unsealer is invoked without re-authorizing
        assert unsealer.calls == [(TEST_MODEL, False)]

    def test_post_migrate_model_reunseals_without_reauthorizing(self) -> None:
        # GIVEN an extension wrapping a stub unsealer, mimicking a model that just migrated
        juju_backend = JujuBackendStub()
        unsealer = VaultUnsealerStub(juju_backend)
        extension = GenericUnsealVaultJujuExtension(unsealer)

        # WHEN post_migrate_model is called (e.g. after migrating between controllers)
        extension.post_migrate_model("test-model", "source-ctrl", "target-ctrl")

        # THEN the extension waits for the model to land on the target controller before
        # re-unsealing vault there, without re-authorizing the already-authorized charm
        target_model = JujuModelHandle(controller="target-ctrl", model="test-model")
        assert juju_backend.wait_calls == [target_model]
        assert unsealer.calls == [(target_model, False)]


class TestUnsealVaultK8sJujuExtension:
    def _build_extension(self) -> tuple[UnsealVaultK8sJujuExtension, JujuBackendStub]:
        juju = JujuBackendStub()
        extension = UnsealVaultK8sJujuExtension(juju, logging.getLogger("test"))
        extension.vault_unsealer = VaultUnsealerStub(juju)
        return extension, juju

    def test_is_kubernetes_extension(self) -> None:
        extension, _ = self._build_extension()

        assert isinstance(extension, KubernetesExtension)
        assert isinstance(extension, JujuExtension)

    def test_inherited_juju_lifecycle_hooks_reuse_kubernetes_unsealer(self) -> None:
        extension, juju = self._build_extension()
        unsealer = extension.vault_unsealer
        assert isinstance(unsealer, VaultUnsealerStub)

        extension.post_deploy(TEST_MODEL)
        extension.post_scale(TEST_MODEL)
        extension.post_restart_unit(TEST_MODEL, "vault-k8s/0")
        extension.post_migrate_model("test-model", "source-controller", "target-controller")

        target_model = JujuModelHandle(controller="target-controller", model="test-model")
        assert unsealer.calls == [
            (TEST_MODEL, True),
            (TEST_MODEL, False),
            (TEST_MODEL, False),
            (target_model, False),
        ]
        assert juju.wait_calls == [target_model]

    def test_post_delete_pod_resolves_model_and_unseals_without_reauthorizing(self) -> None:
        # GIVEN an extension wrapping a stub unsealer, mimicking a vault-k8s pod deletion
        extension, juju = self._build_extension()
        unsealer = extension.vault_unsealer
        assert isinstance(unsealer, VaultUnsealerStub)
        model = JujuModelHandle(controller="litmus-controller", model=TEST_MODEL.model)

        # WHEN a pod is deleted in a namespace on the litmus cloud
        extension.post_delete_pod_in_cloud(TEST_MODEL.model, "vault-k8s-0", "litmus")

        # THEN the unsealer re-unseals vault without re-authorizing the charm
        assert juju.namespace_lookups == [("litmus", TEST_MODEL.model)]
        assert unsealer.calls == [(model, False)]

    def test_post_restart_statefulset_resolves_model_and_unseals_without_reauthorizing(self) -> None:
        extension, juju = self._build_extension()
        unsealer = extension.vault_unsealer
        assert isinstance(unsealer, VaultUnsealerStub)
        model = JujuModelHandle(controller="litmus-controller", model=TEST_MODEL.model)

        extension.post_restart_statefulset_in_cloud(TEST_MODEL.model, "vault-k8s", "litmus")

        assert juju.namespace_lookups == [("litmus", TEST_MODEL.model)]
        assert unsealer.calls == [(model, False)]

    def test_requires_cloud_context_from_registry(self) -> None:
        extension, juju = self._build_extension()

        with pytest.raises(RuntimeError, match="requires a cloud"):
            extension.post_delete_pod_in_cloud(TEST_MODEL.model, "vault-k8s-0", None)

        assert juju.namespace_lookups == []

    def test_ignores_namespace_without_a_juju_model(self) -> None:
        extension, juju = self._build_extension()
        juju.namespace_not_found = True
        unsealer = extension.vault_unsealer
        assert isinstance(unsealer, VaultUnsealerStub)

        extension.post_restart_statefulset_in_cloud("controller-test-controller", "controller", "litmus")

        assert juju.namespace_lookups == [("litmus", "controller-test-controller")]
        assert unsealer.calls == []


def test_unit_restart_unseals_without_reauthorizing() -> None:
    unsealer = VaultUnsealerStub()
    extension = GenericUnsealVaultJujuExtension(unsealer)

    extension.post_restart_unit(TEST_MODEL, "vault/1")

    assert unsealer.calls == [(TEST_MODEL, False)]
