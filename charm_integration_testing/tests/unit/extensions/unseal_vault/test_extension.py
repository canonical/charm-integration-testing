# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta

from extensions.unseal_vault.extensions import GenericUnsealVaultJujuExtension, UnsealVaultK8sJujuExtension
from extensions.unseal_vault.vault_unsealer import VaultUnsealer
from juju import JujuModelHandle
from kubernetes_client.backend import KubernetesExtension

from ..shared import NullJujuBackend

TEST_MODEL = JujuModelHandle(controller="test-controller", model="test-model")


class JujuBackendStub(NullJujuBackend):
    """Stub JujuBackend that records wait_for_model_to_exist calls."""

    def __init__(self) -> None:
        self.wait_calls: list[JujuModelHandle] = []

    def wait_for_model_to_exist(self, model: JujuModelHandle, timeout: timedelta | None) -> None:
        self.wait_calls.append(model)


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
        extension.post_delete_pod(TEST_MODEL.model, "vault-k8s-0")

        # THEN the unsealer re-unseals vault, using a controller-less handle for the namespace,
        # without re-authorizing the already-authorized charm
        assert unsealer.calls == [(JujuModelHandle(model=TEST_MODEL.model), False)]

    def test_post_restart_statefulset_reunseals_without_reauthorizing(self) -> None:
        # GIVEN an extension wrapping a stub unsealer, mimicking a statefulset rollout restart
        extension = self._build_extension()
        unsealer = extension.vault_unsealer
        assert isinstance(unsealer, VaultUnsealerStub)

        # WHEN post_restart_statefulset is called
        extension.post_restart_statefulset(TEST_MODEL.model, "vault-k8s")

        # THEN the unsealer re-unseals vault, using a controller-less handle for the namespace,
        # without re-authorizing the already-authorized charm
        assert unsealer.calls == [(JujuModelHandle(model=TEST_MODEL.model), False)]
