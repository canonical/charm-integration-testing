# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import time
from dataclasses import asdict
from datetime import timedelta
from functools import cmp_to_key
from subprocess import CalledProcessError  # nosec
from typing import Literal

from juju import JujuBackend
from pydantic.dataclasses import dataclass

from .vault_client import VaultClient, VaultStatus, VaultTokenSecret


@dataclass
class CharmInfo:
    name: str
    init_message: str = "Please initialize Vault"
    unseal_message: str = "Please unseal Vault"
    authorize_message: str = "Please authorize charm"
    auto_unseal_requirer_endpoint: str = "vault-autounseal-requires"


def order_apps_by_dependency(applications: list[str], integrations: set[tuple[str, str]]) -> list[str]:
    """Order applications by their dependency relationships.

    Orders a list of applications based on their integration dependencies, ensuring that
    provider applications come before requirer applications.

    When there's no direct or transitive dependency relationship, applications with fewer
    dependencies are ordered before those with more dependencies.

    If a circular dependency is detected, the cycle is broken and ordering continues.

    Original order is preserved when dependency relationships don't determine a clear ordering.

    If there are duplicates in applications, they will end up grouped together.

    Args:
        applications: List of applications to order
        integrations: Set of tuples representing integrations as (provider, requirer) pairs.

    Returns:
        List of applications ordered by dependency, with providers before requirers.
    """
    if len(applications) < 2:
        return applications

    direct_dependencies: dict[str, set[str]] = {}
    for provider, requirer in integrations:
        if (entry := direct_dependencies.get(requirer)) is None:
            direct_dependencies[requirer] = {provider}
        else:
            entry.add(provider)

    full_dependencies: dict[str, set[str] | Literal["visiting"]] = {}

    def dependencies_of(app: str) -> set[str]:
        if (result := full_dependencies.get(app)) is not None:
            # there's a cycle if we hit "visiting"
            return set() if result == "visiting" else result

        full_dependencies[app] = "visiting"
        result = direct_dependencies.get(app, set())
        for dependency in list(result):
            # TODO(@motjuste): consider a maximum recursion depth
            result |= dependencies_of(dependency)

        full_dependencies[app] = result
        return result

    def cmp(app1: str, app2: str) -> int:
        if app1 == app2:
            return 0

        if app1 in (deps2 := dependencies_of(app2)):
            return -1  # order app1 before app2
        if app2 in (deps1 := dependencies_of(app1)):
            return 1  # order app2 before app1

        if len(deps2) > len(deps1):
            return -1  # order app1 before app2 due to fewer dependencies
        if len(deps1) > len(deps2):
            return 1  # order app2 before app1 due to fewer dependencies

        # returning 0 _should_ keep original order
        return 0

    result = sorted(applications, key=cmp_to_key(cmp))
    return result


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

    def try_init_or_unseal_all_vaults(self, model: str, authorize_charm: bool = True) -> None:
        # Look for vault charms
        for application in self.ordered_vaults(model):
            self.try_init_or_unseal_vault(model, application, authorize_charm)

    def ordered_vaults(self, model: str) -> list[str]:
        # collect all vault apps
        vault_apps = sorted(
            (
                app
                for app, info in self.juju.list_applications(model).items()
                if info.charm == self.charm.name
                or (
                    # info.charm may be empty, then we need the expensive request to juju
                    info.charm == "" and self.juju.application_charm(model, app) == self.charm.name
                )
            ),
            reverse=True,  # bias descending order by name (vault, target, neighbor)
        )

        # not enough vaults to bother ordering
        if len(vault_apps) < 2:
            return vault_apps

        # all integrations as tuples: (provider, requirer)
        integrations = set((i.provider.application, i.requirer.application) for i in self.juju.list_integrations(model))
        try:
            return order_apps_by_dependency(vault_apps, integrations)
        except Exception:
            # probably RecursionError but who knows
            self.logger.error(
                "Failed to order applications by dependencies, falling back to reverse alphabetical order"
            )  # will log exception
            return vault_apps

    def vault_app_should_auto_unseal(self, model: str, application: str) -> bool:
        for integration in self.juju.list_integrations(model):
            requirer = integration.requirer
            if requirer.application == application and requirer.endpoint == self.charm.auto_unseal_requirer_endpoint:
                return True
        return False

    def try_init_or_unseal_vault(self, model: str, application: str, authorize_charm: bool = True) -> None:
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

    def try_init_vault(self, model: str, application: str, authorize_charm: bool = True) -> None:
        # Get leader unit
        leader_unit = f"{application}/leader"

        # Check if vault initialized is
        if self.vault.status(model, leader_unit).initialized:
            return

        should_auto_unseal = self.vault_app_should_auto_unseal(model, application)
        if should_auto_unseal:
            # Wait for auto-unseal to finish
            self.logger.info(f"Waiting for vault charm '{self.charm.name}' unit '{leader_unit}' to accept auto-unseal")
            self.wait_for_auto_unseal_acceptance(
                model, leader_unit, timeout=timedelta(minutes=10), poll_interval=timedelta(seconds=10)
            )

        # Wait for initialization message
        self.logger.info(f"Waiting for vault charm '{self.charm.name}' unit '{leader_unit}' init message")
        self.juju.wait_for_unit_message(model, leader_unit, self.charm.init_message, timedelta(minutes=10))

        # Initialize vault
        self.logger.info(f"Initializing vault charm '{self.charm.name}' unit '{leader_unit}'")
        tokens = self.vault.init(model, leader_unit, will_auto_unseal=should_auto_unseal)

        # Save the token as a secret
        self.save_vault_tokens(model, application, tokens)

        if not should_auto_unseal:
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

    def wait_for_auto_unseal_acceptance(
        self, model: str, unit: str, timeout: timedelta, poll_interval: timedelta
    ) -> None:
        remaining = timeout
        while remaining.total_seconds() > 0:
            remaining -= poll_interval
            if self.vault.status(model, unit).will_auto_unseal:
                return
            time.sleep(poll_interval.total_seconds())
        raise TimeoutError(f"Timed out while waiting for '{self.charm.name}' unit {unit} to auto-unseal")

    def try_unseal_vault(self, model: str, application: str) -> None:
        # Get vault tokens
        tokens = self.get_vault_tokens(model, application)

        # Check each unit
        for unit in self.juju.application_units(model, application):
            status = self.vault.status(model, unit)

            # Already unsealed, nothing to do
            if not status.sealed:
                continue

            # Non-leader units join the raft cluster in the background after being scaled up, so
            # `initialized` may briefly still be false. Poll for it instead of giving up immediately,
            # otherwise a slow-to-join unit is permanently skipped and never gets unsealed.
            if not status.initialized:
                self.logger.info(f"Waiting for vault charm '{self.charm.name}' unit '{unit}' to be initialized")
                status = self.wait_for_vault_initialized(
                    model, unit, timeout=timedelta(minutes=10), poll_interval=timedelta(seconds=10)
                )

            if not status.initialized:
                self.logger.info(
                    f"Skipping vault charm '{self.charm.name}' unit '{unit}': "
                    "still not initialized after waiting, will retry on next run"
                )
                continue

            # Wait for unseal message
            self.logger.info(f"Waiting for vault charm '{self.charm.name}' unit '{unit}' unseal message")
            self.juju.wait_for_unit_message(model, unit, self.charm.unseal_message, timedelta(minutes=10))

            # Unseal vault
            self.logger.info(f"Unsealing vault charm '{self.charm.name}' unit '{unit}'")
            self.vault.unseal(model, unit, tokens)

    def wait_for_vault_initialized(
        self, model: str, unit: str, timeout: timedelta, poll_interval: timedelta
    ) -> VaultStatus:
        """Poll a unit's vault status until it reports as initialized, or the timeout elapses.

        Returns the last observed status, whether or not it became initialized in time, so
        callers can decide how to proceed instead of aborting the whole unseal run.
        """
        remaining = timeout
        status = self.vault.status(model, unit)
        while not status.initialized and remaining.total_seconds() > 0:
            sleep_for = min(poll_interval, remaining)
            time.sleep(sleep_for.total_seconds())
            remaining -= sleep_for
            status = self.vault.status(model, unit)
        return status

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

    def save_vault_tokens(self, model: str, application: str, tokens: VaultTokenSecret) -> None:
        # See if vault-token already exists
        secret_name = self.vault_tokens_secret_name(application)
        try:
            self.juju.remove_secret(model, secret_name)
            self.logger.info(f"Removed existing secret '{secret_name}'")
        except CalledProcessError as err:
            self.logger.info(f"Ignoring failure to remove secret '{secret_name}': {err.stderr}")

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
