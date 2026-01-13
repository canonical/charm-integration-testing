# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import time
from dataclasses import asdict
from datetime import timedelta

from juju import JujuBackend
from pydantic.dataclasses import dataclass

from .vault_client import VaultClient, VaultTokenSecret


@dataclass
class CharmInfo:
    name: str
    init_message: str = "Please initialize Vault"
    unseal_message: str = "Please unseal Vault"
    authorize_message: str = "Please authorize charm"


class VaultUnsealer:
    charm: CharmInfo
    vault: VaultClient
    juju: JujuBackend
    logger: logging.Logger

    def __init__(self, charm: CharmInfo, vault: VaultClient, juju: JujuBackend, logger: logging.Logger):
        self.charm = charm
        self.vault = vault
        self.juju = juju
        self.logger = logger

    def try_init_or_unseal_all_vaults(self, model: str, authorize_charm: bool = True):
        # Look for vault charms
        for application in self.juju.list_applications(model):
            if self.juju.application_charm(model, application) == self.charm.name:
                self.try_init_or_unseal_vault(model, application, authorize_charm)

    def try_init_or_unseal_vault(self, model: str, application: str, authorize_charm: bool = True):
        # Wait for application to be scaled
        self.logger.info(f"Waiting for vault charm '{self.charm.name}' application '{application}' to be scaled")
        self.juju.wait_application_scaled(model, application, timedelta(minutes=10))

        # Skip if no units
        if self.juju.num_units(model, application) == 0:
            self.logger.info(f"Vault charm '{self.charm.name}' application '{application}' has no units")
            return

        # Wait for units to settle
        self.logger.info(f"Waiting for vault charm '{self.charm.name}' application '{application}' units to be settled")
        self.juju.wait_application_settled(model, application, timedelta(minutes=10))

        # Try to initialize vault
        self.try_init_vault(model, application, authorize_charm)

        # Try to unseal any sealed units
        self.try_unseal_vault(model, application)

    def try_init_vault(self, model: str, application: str, authorize_charm: bool = True):
        # Get leader unit
        leader_unit = f"{application}/leader"

        # Check if vault initialized is
        if self.vault.status(model, leader_unit).initialized:
            return

        # Wait for initialization message
        self.logger.info(f"Waiting for vault charm '{self.charm.name}' unit '{leader_unit}' init message")
        self.juju.wait_for_unit_message(model, leader_unit, self.charm.init_message, timedelta(minutes=10))

        # Initialize vault
        self.logger.info(f"Initializing vault charm '{self.charm.name}' unit '{leader_unit}'")
        tokens = self.vault.init(model, leader_unit)

        # Save the token as a secret
        self.save_vault_tokens(model, application, tokens)

        # Wait for unseal message
        self.logger.info(f"Waiting for vault charm '{self.charm.name}' unit '{leader_unit}' unseal message")
        self.juju.wait_for_unit_message(model, leader_unit, self.charm.unseal_message, timedelta(minutes=10))

        # Unseal the leader
        self.logger.info(f"Unsealing vault charm '{self.charm.name}' unit '{leader_unit}'")
        self.vault.unseal(model, leader_unit, tokens)

        if not authorize_charm:
            self.logger.info(f"Skipping authorizing vault charm '{self.charm.name}' unit '{leader_unit}'")
            return

        # Wait for authorize message
        self.logger.info(f"Waiting for vault charm '{self.charm.name}' unit '{leader_unit}' authorize message")
        self.juju.wait_for_unit_message(model, leader_unit, self.charm.authorize_message, timedelta(minutes=10))

        # Authorize the charm
        self.authorize_vault_charm(model, application, tokens)

    def try_unseal_vault(self, model: str, application: str) -> None:
        # Get vault tokens
        tokens = self.get_vault_tokens(model, application)

        # Check each unit
        for unit in self.juju.application_units(model, application):
            # Only attempt unseal if vault is initialized and sealed
            status = self.vault.status(model, unit)
            if not (status.initialized and status.sealed):
                continue

            # Wait for unseal message
            self.logger.info(f"Waiting for vault charm '{self.charm.name}' unit '{unit}' unseal message")
            self.juju.wait_for_unit_message(model, unit, self.charm.unseal_message, timedelta(minutes=10))

            # Unseal vault
            self.logger.info(f"Unsealing vault charm '{self.charm.name}' unit '{unit}'")
            self.vault.unseal(model, unit, tokens)

    def authorize_vault_charm(self, model: str, application: str, tokens: VaultTokenSecret) -> None:
        # Log
        self.logger.info(f"Authorizing vault charm '{self.charm.name}' application '{application}'")

        # Create the one time secret
        secret_id = self.juju.add_secret(
            model, self.vault_one_time_token_secret_name(application), {"token": tokens.root_token}
        )

        # Grant the charm access to the one time secret
        self.juju.grant_secret(model, self.vault_one_time_token_secret_name(application), application)

        # Authorize the charm (multiple attempts since the secret granting may have propagation delay)
        exception: Exception
        for _ in range(3):
            time.sleep(3)
            try:
                self.juju.run_action(model, f"{application}/leader", "authorize-charm", {"secret-id": secret_id})
                break
            except Exception as e:
                exception = e
        else:
            raise exception

        # Remove the one time secret
        self.juju.remove_secret(model, self.vault_one_time_token_secret_name(application))

    def save_vault_tokens(self, model: str, application: str, tokens: VaultTokenSecret):
        # See if vault-token already exists
        secret_name = self.vault_tokens_secret_name(application)
        try:
            self.juju.remove_secret(model, secret_name)
            self.logger.info(f"Removed existing secret '{secret_name}'")
        except Exception as err:  # TODO(@motjuste): or (juju.CliError | subprocess.CalledProcessError)
            self.logger.info(f"Ignoring failure to remove secret '{secret_name}': {getattr(err, 'stderr', err)}")

        # Add the vault tokens
        self.juju.add_secret(
            model,
            secret_name,
            {key.replace("_", "-"): value for key, value in asdict(tokens).items()},
        )

    def get_vault_tokens(self, model: str, application: str) -> VaultTokenSecret:
        # Parse the vault tokens
        return VaultTokenSecret(
            **{
                key.replace("-", "_"): value
                for key, value in self.juju.read_secret(model, self.vault_tokens_secret_name(application)).items()
            }
        )

    @staticmethod
    def vault_tokens_secret_name(application: str) -> str:
        return f"vault-secret-application-{application}-tokens"

    @staticmethod
    def vault_one_time_token_secret_name(application: str) -> str:
        return f"vault-secret-application-{application}-one-time-token"
