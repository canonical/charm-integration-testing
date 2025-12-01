# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from datetime import timedelta

from extensions.unseal_vault.vault_client import VaultStatus
from extensions.unseal_vault.vault_unsealer import CharmInfo, VaultUnsealer
from extensions.unseal_vault.vault_client import VaultTokenSecret


@dataclass
class JujuStub:
    apps: list[str] = field(default_factory=list)
    charm_name: str = ""
    scaled_apps: list[str] = field(default_factory=list)
    settled_apps: list[str] = field(default_factory=list)
    units: dict[str, list[str]] = field(default_factory=dict)
    messages: list[tuple[str, str, str, timedelta]] = field(default_factory=list)
    secrets: dict[str, dict] = field(default_factory=dict)
    secrets_granted: list[tuple[str, str]] = field(default_factory=list)
    actions_run: list[tuple[str, str, dict]] = field(default_factory=list)

    def list_applications(self, model: str) -> list[str]:
        return self.apps

    def application_charm(self, model: str, application: str) -> str:
        return self.charm_name

    def wait_application_scaled(self, model: str, app: str, timeout: timedelta) -> None:
        self.scaled_apps.append(app)

    def wait_application_settled(self, model: str, app: str, timeout: timedelta) -> None:
        self.settled_apps.append(app)

    def application_units(self, model: str, app: str) -> list[str]:
        return self.units.get(app, [])

    def num_units(self, model: str, app: str) -> int:
        return len(self.units.get(app, []))

    def wait_for_unit_message(self, model: str, unit: str, message: str, timeout: timedelta) -> None:
        self.messages.append((unit, message, timeout))

    def add_secret(self, model: str, name: str, content: dict) -> str:
        self.secrets[name] = content
        return "secret-id"

    def grant_secret(self, model: str, name: str, app: str) -> None:
        self.secrets_granted.append((name, app))

    def run_action(self, model: str, unit: str, action: str, params: dict) -> None:
        self.actions_run.append((unit, action, params))

    def remove_secret(self, model: str, name: str) -> None:
        del self.secrets[name]

    def read_secret(self, model: str, name: str) -> dict:
        return self.secrets[name]


@dataclass
class VaultStub:
    initialized_units: dict[str, bool] = field(default_factory=dict)
    sealed_units: dict[str, bool] = field(default_factory=dict)
    inits: list[str] = field(default_factory=list)
    unseals: list[str] = field(default_factory=list)
    tokens: VaultTokenSecret = field(default_factory=lambda: VaultTokenSecret(root_token="root", unseal_key="key"))

    def status(self, model: str, unit: str) -> VaultStatus:
        return VaultStatus(
            initialized=self.initialized_units.get(unit, False),
            sealed=self.sealed_units.get(unit, True),
        )

    def init(self, model: str, unit: str) -> VaultTokenSecret:
        self.inits.append(unit)
        return self.tokens

    def unseal(self, model: str, unit: str, tokens: VaultTokenSecret) -> None:
        self.unseals.append(unit)


class LoggerStub:
    def __init__(self) -> None:
        self.messages = []

    def info(self, message: str) -> None:
        self.messages.append(message)


class TestVaultUnsealer:
    def test_try_init_or_unseal_all_vaults(self) -> None:
        # GIVEN
        juju = JujuStub(apps=["vault"], charm_name="vault", units={"vault": ["vault/leader"]})
        vault = VaultStub(initialized_units={"vault/leader": False})
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_init_or_unseal_all_vaults("test-model")

        # THEN
        assert "vault" in juju.scaled_apps
        assert "vault/leader" in vault.inits

    def test_try_init_vault_skips_if_already_initialized(self) -> None:
        # GIVEN
        juju = JujuStub(units={"vault": ["vault/leader"]})
        vault = VaultStub(initialized_units={"vault/leader": True})
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_init_vault("test-model", "vault")

        # THEN
        assert vault.inits == []
        assert vault.unseals == []

    def test_try_unseal_vault_unseals_if_initialized_and_sealed(self) -> None:
        # GIVEN
        juju = JujuStub(
            units={"vault": ["vault/0", "vault/1"]},
            secrets={
                "vault-secret-application-vault-tokens": {"root-token": "abc", "unseal-key": "xyz"},
            },
        )
        vault = VaultStub(
            initialized_units={"vault/0": True, "vault/1": True}, sealed_units={"vault/0": True, "vault/1": False}
        )
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_unseal_vault("test-model", "vault")

        # THEN
        assert "vault/0" in vault.unseals
        assert "vault/1" not in vault.unseals

    def test_authorize_vault_charm_runs_action_and_removes_secret(self) -> None:
        # GIVEN
        juju = JujuStub()
        vault = VaultStub()
        logger = LoggerStub()
        charm = CharmInfo(name="vault")
        tokens = VaultTokenSecret(root_token="abc", unseal_key="xyz")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).authorize_vault_charm("test-model", "vault", tokens)

        # THEN
        assert ("vault-secret-application-vault-one-time-token", "vault") in juju.secrets_granted
        assert ("vault/leader", "authorize-charm", {"secret-id": "secret-id"}) in juju.actions_run
        assert "vault-secret-application-vault-one-time-token" not in juju.secrets

    def test_save_and_get_vault_tokens(self) -> None:
        # GIVEN
        juju = JujuStub()
        vault = VaultStub()
        logger = LoggerStub()
        charm = CharmInfo(name="vault")
        tokens = VaultTokenSecret(root_token="abc", unseal_key="xyz")
        unsealer = VaultUnsealer(charm, vault, juju, logger)

        # WHEN
        unsealer.save_vault_tokens("test-model", "vault", tokens)
        result = unsealer.get_vault_tokens("test-model", "vault")

        # THEN
        assert juju.secrets["vault-secret-application-vault-tokens"] == {"root-token": "abc", "unseal-key": "xyz"}
        assert result == tokens
