# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from datetime import timedelta

from extensions.unseal_vault.vault_unsealer import CharmInfo, VaultTokenSecret, VaultUnsealer


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

    def list_applications(self, model: str):
        return self.apps

    def application_charm(self, model: str, application: str):
        return self.charm_name

    def wait_application_scaled(self, model, app, timeout):
        self.scaled_apps.append(app)

    def wait_application_settled(self, model, app, timeout):
        self.settled_apps.append(app)

    def application_units(self, model, app):
        return self.units.get(app, [])

    def num_units(self, model, app):
        return len(self.units.get(app, []))

    def wait_for_unit_message(self, model, unit, message, timeout):
        self.messages.append((unit, message, timeout))

    def add_secret(self, model, name, content):
        self.secrets[name] = content
        return "secret-id"

    def grant_secret(self, model, name, app):
        self.secrets_granted.append((name, app))

    def run_action(self, model, unit, action, params):
        self.actions_run.append((unit, action, params))

    def remove_secret(self, model, name):
        del self.secrets[name]

    def read_secret(self, model, name):
        return self.secrets[name]


@dataclass
class VaultStub:
    initialized_units: dict[str, bool] = field(default_factory=dict)
    sealed_units: dict[str, bool] = field(default_factory=dict)
    inits: list[str] = field(default_factory=list)
    unseals: list[str] = field(default_factory=list)
    tokens: VaultTokenSecret = field(default_factory=lambda: VaultTokenSecret(root_token="root", unseal_key="key"))

    def status(self, model, unit):
        return type(
            "Status",
            (),
            {
                "initialized": self.initialized_units.get(unit, False),
                "sealed": self.sealed_units.get(unit, True),
            },
        )()

    def init(self, model, unit):
        self.inits.append(unit)
        return self.tokens

    def unseal(self, model, unit, tokens):
        self.unseals.append(unit)


class LoggerStub:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


class TestVaultUnsealer:
    def test_try_init_or_unseal_all_vaults(self):
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

    def test_try_init_vault_skips_if_already_initialized(self):
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

    def test_try_unseal_vault_unseals_if_initialized_and_sealed(self):
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

    def test_authorize_vault_charm_runs_action_and_removes_secret(self):
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

    def test_save_and_get_vault_tokens(self):
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

    def test_vault_status_retries_on_connection_refused(self):
        # GIVEN
        juju = JujuStub(units={"vault": ["vault/leader"]})
        vault = VaultStub()

        def raise_connection_refused(model, unit):
            raise RuntimeError(
                'ERROR Failure in test_deploy: RuntimeError: Failed to query vault status: \
                         Error checking seal status: Get "https://127.0.0.1:8200/v1/sys/seal-status": dial tcp 127.0.0.1:8200: connect: connection refused'
            )

        vault.status = raise_connection_refused
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        try:
            VaultUnsealer(charm, vault, juju, logger).try_init_vault("test-model", "vault")
        # THEN
        except RuntimeError as e:
            assert "connection refused" in str(e).lower()
        else:
            assert False, "Expected RuntimeError was not raised"

    def test_vault_status_does_not_retry_on_other_errors(self):
        # GIVEN
        juju = JujuStub(units={"vault": ["vault/leader"]})
        vault = VaultStub()

        def raise_other_error(model, unit):
            raise RuntimeError(
                "ERROR Failure in test_deploy: RuntimeError: Failed to query vault status: Some other error occurred"
            )

        vault.status = raise_other_error
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        try:
            VaultUnsealer(charm, vault, juju, logger).try_init_vault("test-model", "vault")
        # THEN
        except RuntimeError as e:
            assert "some other error occurred" in str(e).lower()
        else:
            assert False, "Expected RuntimeError was not raised"
