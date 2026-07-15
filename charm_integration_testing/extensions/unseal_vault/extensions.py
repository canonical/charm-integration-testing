# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from abc import ABC

from juju import JujuBackend, JujuExtension
from kubernetes_client.backend import KubernetesExtension

from .vault_client import VaultClientJujuExec, VaultClientJujuExecPebble
from .vault_unsealer import CharmInfo, VaultUnsealer


class GenericUnsealVaultJujuExtension(JujuExtension, ABC):
    vault_unsealer: VaultUnsealer

    def __init__(self, vault_unsealer: VaultUnsealer) -> None:
        self.vault_unsealer = vault_unsealer

    def post_deploy(self, model: str) -> None:
        self.vault_unsealer.try_init_or_unseal_all_vaults(model, authorize_charm=True)

    def post_scale(self, model: str) -> None:
        self.vault_unsealer.try_init_or_unseal_all_vaults(model, authorize_charm=False)

    def post_migrate_model(self, model: str, source: str, target: str) -> None:
        # Model migration triggers a StatefulSet annotation update on k8s, restarting the
        # vault pod. Vault comes back sealed, so it needs re-unsealing (it's already
        # initialized and authorized, so authorize_charm=False mirrors post_scale).
        self.vault_unsealer.try_init_or_unseal_all_vaults(model, authorize_charm=False)


class UnsealVaultJujuExtension(GenericUnsealVaultJujuExtension):
    def __init__(self, juju: JujuBackend, logger: logging.Logger) -> None:
        super().__init__(VaultUnsealer(CharmInfo(name="vault"), VaultClientJujuExec(juju), juju, logger))


class UnsealVaultK8sJujuExtension(GenericUnsealVaultJujuExtension, KubernetesExtension):
    def __init__(self, juju: JujuBackend, logger: logging.Logger) -> None:
        super().__init__(VaultUnsealer(CharmInfo(name="vault-k8s"), VaultClientJujuExecPebble(juju), juju, logger))

    def post_delete_pod(self, namespace: str, _pod_name: str) -> None:
        # A deleted vault-k8s pod comes back sealed (Vault's in-memory unseal state is lost
        # on process restart). Re-unseal without re-authorizing, mirroring post_scale.
        self.vault_unsealer.try_init_or_unseal_all_vaults(namespace, authorize_charm=False)

    def post_restart_statefulset(self, namespace: str, _statefulset_name: str) -> None:
        # Same rationale as post_delete_pod: a StatefulSet rollout restarts every pod,
        # which seals vault-k8s again.
        self.vault_unsealer.try_init_or_unseal_all_vaults(namespace, authorize_charm=False)
