# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from abc import ABC
from datetime import timedelta

from juju import JujuBackend, JujuExtension, JujuModelHandle

from .vault_client import VaultClientJujuExec, VaultClientJujuExecPebble
from .vault_unsealer import CharmInfo, VaultUnsealer


class GenericUnsealVaultJujuExtension(JujuExtension, ABC):
    vault_unsealer: VaultUnsealer

    def __init__(self, vault_unsealer: VaultUnsealer) -> None:
        self.vault_unsealer = vault_unsealer

    def post_deploy(self, model: JujuModelHandle) -> None:
        self.vault_unsealer.try_init_or_unseal_all_vaults(model, authorize_charm=True)

    def post_scale(self, model: JujuModelHandle) -> None:
        self.vault_unsealer.try_init_or_unseal_all_vaults(model, authorize_charm=False)

    def post_restart_unit(self, model: JujuModelHandle, unit: str) -> None:
        self.vault_unsealer.try_init_or_unseal_all_vaults(model, authorize_charm=False)

    def post_migrate_model(self, model: str, _source: str, target: str) -> None:
        # Vault comes back sealed after migration; re-unseal without re-authorizing.
        # Wait for the model on the target controller first: migrate_model() returns
        # as soon as migration starts, so an immediate query can race the move.
        target_model = JujuModelHandle(controller=target, model=model)
        self.vault_unsealer.juju.wait_for_model_to_exist(target_model, timeout=timedelta(minutes=15))
        self.vault_unsealer.try_init_or_unseal_all_vaults(target_model, authorize_charm=False)


class UnsealVaultJujuExtension(GenericUnsealVaultJujuExtension):
    def __init__(self, juju: JujuBackend, logger: logging.Logger) -> None:
        super().__init__(VaultUnsealer(CharmInfo(name="vault"), VaultClientJujuExec(juju), juju, logger))


class UnsealVaultK8sJujuExtension(GenericUnsealVaultJujuExtension):
    def __init__(self, juju: JujuBackend, logger: logging.Logger) -> None:
        super().__init__(VaultUnsealer(CharmInfo(name="vault-k8s"), VaultClientJujuExecPebble(juju), juju, logger))

    def post_delete_pod(self, model: JujuModelHandle, _pod_name: str) -> None:
        self.vault_unsealer.try_init_or_unseal_all_vaults(model, authorize_charm=False)
