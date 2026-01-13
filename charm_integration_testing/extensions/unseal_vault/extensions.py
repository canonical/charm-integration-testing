# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from abc import ABC

from juju import JujuBackend, JujuExtension

from .vault_client import VaultClientJujuExec, VaultClientJujuExecPebble
from .vault_unsealer import CharmInfo, VaultUnsealer


class GenericUnsealVaultJujuExtension(JujuExtension, ABC):
    vault_unsealer: VaultUnsealer

    def __init__(self, vault_unsealer: VaultUnsealer) -> None:
        self.vault_unsealer = vault_unsealer

    def post_deploy(self, model: str):
        self.vault_unsealer.try_init_or_unseal_all_vaults(model, authorize_charm=True)

    def post_scale(self, model: str):
        self.vault_unsealer.try_init_or_unseal_all_vaults(model, authorize_charm=False)


class UnsealVaultJujuExtension(GenericUnsealVaultJujuExtension):
    def __init__(self, juju: JujuBackend, logger: logging.Logger) -> None:
        super().__init__(VaultUnsealer(CharmInfo(name="vault"), VaultClientJujuExec(juju), juju, logger))


class UnsealVaultK8sJujuExtension(GenericUnsealVaultJujuExtension):
    def __init__(self, juju: JujuBackend, logger: logging.Logger):
        super().__init__(VaultUnsealer(CharmInfo(name="vault-k8s"), VaultClientJujuExecPebble(juju), juju, logger))
