# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from extensions.unseal_vault.vault_client import VaultClient, VaultStatus, VaultTokenSecret
from extensions.unseal_vault.vault_unsealer import CharmInfo, VaultUnsealer
from juju.backend import JujuBackend, JujuTask


@dataclass
class JujuStub(JujuBackend):
    apps: list[str] = field(default_factory=list)
    charm_name: str = ""
    scaled_apps: list[str] = field(default_factory=list)
    settled_apps: list[str] = field(default_factory=list)
    units: dict[str, list[str]] = field(default_factory=dict)
    messages: list[tuple[str, str, timedelta]] = field(default_factory=list)
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    secrets_granted: list[tuple[str, str]] = field(default_factory=list)
    actions_run: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)

    def list_applications(self, model: str) -> list[str]:  # type: ignore[override]
        return self.apps

    def application_charm(self, model: str, application: str) -> str:
        return self.charm_name

    def wait_application_scaled(self, model: str, app: str, timeout: timedelta) -> None:  # type: ignore[override]
        self.scaled_apps.append(app)

    def wait_application_settled(self, model: str, app: str, timeout: timedelta) -> None:  # type: ignore[override]
        self.settled_apps.append(app)

    def application_units(self, model: str, app: str) -> list[str]:
        return self.units.get(app, [])

    def num_units(self, model: str, app: str) -> int:
        return len(self.units.get(app, []))

    def wait_for_unit_message(self, model: str, unit: str, message: str, timeout: timedelta) -> None:  # type: ignore[override]
        self.messages.append((unit, message, timeout))

    def add_secret(self, model: str, name: str, content: dict[str, str]) -> str:
        self.secrets[name] = content
        return "secret-id"

    def grant_secret(self, model: str, name: str, app: str) -> None:
        self.secrets_granted.append((name, app))

    def run_action(self, model: str, unit: str, action: str, params: dict[str, Any]) -> JujuTask:
        self.actions_run.append((unit, action, params))
        return JujuTask()  # Dummy; provided to satisfy return type

    def remove_secret(self, model: str, name: str) -> None:
        try:
            del self.secrets[name]
        except KeyError as err:
            raise subprocess.CalledProcessError(-1, ["remove", "unknown"], stderr="did not find it") from err

    def read_secret(self, model: str, name: str) -> dict[str, str]:
        return self.secrets[name]

    def scale_application(self) -> None:  # type: ignore[override]
        pass

    def list_integrations(self) -> None:  # type: ignore[override]
        pass

    def integration_exists(self) -> None:  # type: ignore[override]
        pass

    def wait_idle(self) -> None:  # type: ignore[override]
        pass

    def juju_status_text(self) -> None:  # type: ignore[override]
        pass

    def integrate(self) -> None:  # type: ignore[override]
        pass

    def remove_integration(self) -> None:  # type: ignore[override]
        pass

    def deploy_bundle_file(self) -> None:  # type: ignore[override]
        pass

    def remove_applications(self) -> None:  # type: ignore[override]
        pass

    def wait_for_removal(self) -> None:  # type: ignore[override]
        pass

    def wait_for_removal_of_integration(self) -> None:  # type: ignore[override]
        pass

    def wait_for_removal_of_units(self) -> None:  # type: ignore[override]
        pass

    def exec_unit(self) -> None:  # type: ignore[override]
        pass

    def deploy_application(self) -> None:  # type: ignore[override]
        pass

    def configure_application(self) -> None:  # type: ignore[override]
        pass

    def scp(self) -> None:  # type: ignore[override]
        pass

    def ssh(self) -> None:  # type: ignore[override]
        pass

    def unit_ip(self) -> None:  # type: ignore[override]
        pass

    def get_charm_revisions(self) -> None:  # type: ignore[override]
        pass

    def version(self) -> None:  # type: ignore[override]
        pass

    def get_application_config(self, model: str, application: str) -> None:  # type: ignore[override]
        pass


@dataclass
class VaultStub(VaultClient):
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


class LoggerStub(logging.Logger):
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:  # type: ignore[override]
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

    def test_try_init_vault_authorizes_charm_by_default(self) -> None:
        # GIVEN
        juju = JujuStub(units={"vault": ["vault/leader"]})
        vault = VaultStub(initialized_units={"vault/leader": False})
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_init_vault("test-model", "vault")

        # THEN
        assert ("vault/leader", "authorize-charm", {"secret-id": "secret-id"}) in juju.actions_run

    def test_try_init_vault_wont_authorize_charm_if_asked(self) -> None:
        # GIVEN
        juju = JujuStub(units={"vault": ["vault/leader"]})
        vault = VaultStub(initialized_units={"vault/leader": False})
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_init_vault("test-model", "vault", authorize_charm=False)

        # THEN
        for target, action, _ in juju.actions_run:
            assert (target, action) != ("vault/leader", "authorize-charm")

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

    def test_saving_vault_tokens_overwrites_existing(self) -> None:
        # GIVEN
        juju = JujuStub()
        vault = VaultStub()
        logger = LoggerStub()
        charm = CharmInfo(name="vault")
        unsealer = VaultUnsealer(charm, vault, juju, logger)
        tokens_1 = VaultTokenSecret(root_token="abc", unseal_key="xyz")
        tokens_2 = VaultTokenSecret(root_token="efg", unseal_key="jkl")

        # WHEN
        unsealer.save_vault_tokens("test-model", "vault", tokens_1)
        unsealer.save_vault_tokens("test-model", "vault", tokens_2)
        result = unsealer.get_vault_tokens("test-model", "vault")

        # THEN
        assert juju.secrets["vault-secret-application-vault-tokens"] == {"root-token": "efg", "unseal-key": "jkl"}
        assert result == tokens_2

    def test_vault_status_retries_on_connection_refused(self) -> None:
        # GIVEN
        juju = JujuStub(units={"vault": ["vault/leader"]})
        vault = VaultStub()

        def raise_connection_refused(model: str, unit: str) -> VaultStatus:
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

    def test_vault_status_does_not_retry_on_other_errors(self) -> None:
        # GIVEN
        juju = JujuStub(units={"vault": ["vault/leader"]})
        vault = VaultStub()

        def raise_other_error(model: str, unit: str) -> VaultStatus:
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
