# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

from extensions.unseal_vault.extensions import GenericUnsealVaultJujuExtension, UnsealVaultK8sJujuExtension
from extensions.unseal_vault.vault_unsealer import VaultUnsealer
from kubernetes_client.backend import KubernetesExtension

from ..shared import NullJujuBackend


class VaultUnsealerStub(VaultUnsealer):
    """Stub VaultUnsealer that records calls instead of touching Juju/Vault."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def try_init_or_unseal_all_vaults(self, model: str, authorize_charm: bool = True) -> None:
        self.calls.append((model, authorize_charm))


class TestGenericUnsealVaultJujuExtension:
    def test_post_deploy_initializes_and_authorizes(self) -> None:
        # GIVEN an extension wrapping a stub unsealer
        unsealer = VaultUnsealerStub()
        extension = GenericUnsealVaultJujuExtension(unsealer)

        # WHEN post_deploy is called
        extension.post_deploy("test-model")

        # THEN the unsealer is invoked with authorization enabled
        assert unsealer.calls == [("test-model", True)]

    def test_post_scale_unseals_without_reauthorizing(self) -> None:
        # GIVEN an extension wrapping a stub unsealer
        unsealer = VaultUnsealerStub()
        extension = GenericUnsealVaultJujuExtension(unsealer)

        # WHEN post_scale is called
        extension.post_scale("test-model")

        # THEN the unsealer is invoked without re-authorizing
        assert unsealer.calls == [("test-model", False)]

    def test_post_migrate_model_reunseals_without_reauthorizing(self) -> None:
        # GIVEN an extension wrapping a stub unsealer, mimicking a model that just migrated
        unsealer = VaultUnsealerStub()
        extension = GenericUnsealVaultJujuExtension(unsealer)

        # WHEN post_migrate_model is called (e.g. after migrating between controllers)
        extension.post_migrate_model("test-model", source="source-ctrl", target="target-ctrl")

        # THEN the unsealer re-unseals vault without re-authorizing the already-authorized charm
        assert unsealer.calls == [("test-model", False)]


class TestUnsealVaultK8sJujuExtension:
    def _build_extension(self) -> UnsealVaultK8sJujuExtension:
        extension = UnsealVaultK8sJujuExtension(NullJujuBackend(), logging.getLogger("test"))
        extension.vault_unsealer = VaultUnsealerStub()
        return extension

    def test_is_also_a_kubernetes_extension(self) -> None:
        # GIVEN the k8s vault extension
        extension = self._build_extension()

        # THEN it can be used wherever a KubernetesExtension is expected
        assert isinstance(extension, KubernetesExtension)

    def test_post_delete_pod_reunseals_without_reauthorizing(self) -> None:
        # GIVEN an extension wrapping a stub unsealer, mimicking a vault-k8s pod deletion
        extension = self._build_extension()
        unsealer = extension.vault_unsealer
        assert isinstance(unsealer, VaultUnsealerStub)

        # WHEN post_delete_pod is called (e.g. after a pod is force-deleted by a test)
        extension.post_delete_pod("test-model", "vault-k8s-0")

        # THEN the unsealer re-unseals vault, using the namespace as the model, without
        # re-authorizing the already-authorized charm
        assert unsealer.calls == [("test-model", False)]

    def test_post_restart_statefulset_reunseals_without_reauthorizing(self) -> None:
        # GIVEN an extension wrapping a stub unsealer, mimicking a statefulset rollout restart
        extension = self._build_extension()
        unsealer = extension.vault_unsealer
        assert isinstance(unsealer, VaultUnsealerStub)

        # WHEN post_restart_statefulset is called
        extension.post_restart_statefulset("test-model", "vault-k8s")

        # THEN the unsealer re-unseals vault, using the namespace as the model, without
        # re-authorizing the already-authorized charm
        assert unsealer.calls == [("test-model", False)]
