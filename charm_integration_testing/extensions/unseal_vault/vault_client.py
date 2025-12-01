# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC, abstractmethod
from dataclasses import asdict

import yaml
from juju import JujuBackend
from pydantic.dataclasses import dataclass


@dataclass
class VaultInitResponse:
    root_token: str
    unseal_keys_b64: list[str]


@dataclass
class VaultTokenSecret:
    root_token: str
    unseal_key: str


@dataclass
class VaultStatus:
    initialized: bool
    sealed: bool


class VaultClient(ABC):
    @abstractmethod
    def status(self, model: str, unit: str) -> VaultStatus:
        raise NotImplementedError

    @abstractmethod
    def init(self, model: str, unit: str) -> VaultTokenSecret:
        raise NotImplementedError

    @abstractmethod
    def unseal(self, model: str, unit: str, tokens: VaultTokenSecret) -> None:
        raise NotImplementedError


class VaultClientJujuExec(VaultClient):
    JUJU_EXEC_VAULT_STATUS = "VAULT_SKIP_VERIFY=true vault status -format=yaml"
    JUJU_EXEC_VAULT_INIT = "VAULT_SKIP_VERIFY=true vault operator init -format=yaml -key-shares=1 -key-threshold=1"
    JUJU_EXEC_VAULT_UNSEAL = 'VAULT_SKIP_VERIFY=true VAULT_TOKEN="{root_token}" vault operator unseal "{unseal_key}"'

    juju: JujuBackend

    def __init__(self, juju: JujuBackend):
        self.juju = juju

    def status(self, model: str, unit: str) -> VaultStatus:
        # Unseal the vault
        result = self.juju.exec_unit(model, unit, self.JUJU_EXEC_VAULT_STATUS)

        # Check for error
        # Use stderr because non-init/sealed vault returns code != 0
        if result.stderr != "":
            raise RuntimeError(f"Failed to query vault status: {result.stderr}")

        # Parse response
        return VaultStatus(**yaml.safe_load(result.stdout))

    def init(self, model: str, unit: str) -> VaultTokenSecret:
        # Initialize vault
        init_result = self.juju.exec_unit(model, unit, self.JUJU_EXEC_VAULT_INIT)

        # Check for error
        if init_result.return_code != 0:
            raise RuntimeError(f"Failed to initialize vault: {init_result.stderr}")

        # Parse result
        init_response = VaultInitResponse(**yaml.safe_load(init_result.stdout))

        # Return tokens
        return VaultTokenSecret(root_token=init_response.root_token, unseal_key=init_response.unseal_keys_b64[0])

    def unseal(self, model: str, unit: str, tokens: VaultTokenSecret) -> None:
        # Unseal the vault
        unseal_result = self.juju.exec_unit(model, unit, self.JUJU_EXEC_VAULT_UNSEAL.format(**asdict(tokens)))

        # Check for error
        if unseal_result.return_code != 0:
            raise RuntimeError(f"Failed to unseal vault: {unseal_result.stderr}")


class VaultClientJujuExecPebble(VaultClientJujuExec):
    JUJU_EXEC_VAULT_STATUS = "PEBBLE_SOCKET=/charm/containers/vault/pebble.socket pebble exec --env VAULT_SKIP_VERIFY=true -- vault status -format=yaml"
    JUJU_EXEC_VAULT_INIT = "PEBBLE_SOCKET=/charm/containers/vault/pebble.socket pebble exec --env VAULT_SKIP_VERIFY=true -- vault operator init -format=yaml -key-shares=1 -key-threshold=1"
    JUJU_EXEC_VAULT_UNSEAL = 'PEBBLE_SOCKET=/charm/containers/vault/pebble.socket pebble exec --env VAULT_SKIP_VERIFY=true --env VAULT_TOKEN="{root_token}" -- vault operator unseal "{unseal_key}"'
