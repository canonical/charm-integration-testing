# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
from datetime import timedelta

from juju import JujuBackend, JujuModelHandle
from pydantic.dataclasses import dataclass

from .database_client import DatabaseClient


@dataclass
class CharmInfo:
    name: str
    offer_endpoint: str
    consumer_endpoint: str


class DatabaseReplicator:
    juju: JujuBackend
    logger: logging.Logger
    database_client: DatabaseClient

    def __init__(
        self, charm_info: CharmInfo, juju: JujuBackend, logger: logging.Logger, database_client: DatabaseClient
    ):
        self.juju = juju
        self.logger = logger
        self.charm_info = charm_info
        self.database_client = database_client

    def try_replicate_all_database_clusters(self, model: JujuModelHandle) -> None:
        # Look for database charms
        database_applications = set()

        for application in self.juju.list_applications(model):
            if self.juju.application_charm(model, application) == self.charm_info.name:
                database_applications.add(application)

        if len(database_applications) < 2:
            # Skip if there are not 2+ database units deployed.
            return

        for application1 in database_applications:
            for application2 in database_applications:
                if application1 != application2:
                    if self.juju.integration_exists(
                        application1,
                        self.charm_info.offer_endpoint,
                        application2,
                        self.charm_info.consumer_endpoint,
                        model,
                    ):
                        self.logger.info(f"Found replication integration between {application1} and {application2}.")
                        self.try_replicate_database_cluster(
                            model=model, application_offer=application1, application_consumer=application2
                        )

    def try_replicate_database_cluster(
        self, model: JujuModelHandle, application_offer: str, application_consumer: str
    ) -> None:
        # Wait for application to be scaled
        self.logger.info(
            f"Waiting for database charm '{self.charm_info.name}' application '{application_offer}' to be scaled"
        )
        self.juju.wait_application_scaled(model, application_offer, timedelta(minutes=10))

        # Wait for units to settle
        self.logger.info(
            f"Waiting for database charm '{self.charm_info.name}' application '{application_offer}' units to be settled"
        )
        self.juju.wait_application_settled(model, application_offer, timedelta(minutes=10))

        # Skip if no units
        if self.juju.num_units(model, application_offer) == 0:
            self.logger.info(f"Skipping replication config as no units for {application_offer} were found.")
            return

        # Wait for consumer application to be scaled and settled
        self.logger.info(
            f"Waiting for database charm '{self.charm_info.name}' application '{application_consumer}' to be scaled"
        )
        self.juju.wait_application_scaled(model, application_consumer, timedelta(minutes=10))

        self.logger.info(
            f"Waiting for database charm '{self.charm_info.name}' application '{application_consumer}' units to be settled"
        )
        self.juju.wait_application_settled(model, application_consumer, timedelta(minutes=10))

        # Skip if consumer has no units
        if self.juju.num_units(model, application_consumer) == 0:
            self.logger.info(f"Skipping replication config as no units for {application_consumer} were found.")
            return

        self.logger.info(
            f"Running database replication extension for consumer application {application_consumer} and offer application {application_offer}."
        )
        # Find common databases between offer and consumer
        common_databases = self.find_common_databases(model, application_offer, application_consumer)

        if not common_databases:
            self.logger.info(
                f"No common databases found between '{application_offer}' and '{application_consumer}'. Skipping replication configuration."
            )
            return

        # Build subscription request for all tables in common databases
        subscription_request = {}
        for database in common_databases:
            tables = self.database_client.get_tables(model, application_offer, database)
            if tables:
                subscription_request[database] = tables
                self.logger.info(
                    f"Found {len(tables)} tables in database '{database}' for replication: {', '.join(tables)}"
                )

        if not subscription_request:
            self.logger.info(
                f"No tables found to replicate between '{application_offer}' and '{application_consumer}'."
            )
            return

        # Configure logical replication subscription
        subscription_json = json.dumps(subscription_request)
        self.logger.info(
            f"Configuring logical replication on '{application_consumer}' with subscription: {subscription_json}"
        )
        config_key = self.database_client.get_replication_config_key()
        self.juju.configure_application(model, application_consumer, {config_key: subscription_json})

        # Wait for configuration to be applied
        self.logger.info(f"Waiting for '{application_consumer}' to settle after replication configuration")
        self.juju.wait_application_settled(model, application_consumer, timedelta(minutes=10))

    def find_common_databases(
        self, model: JujuModelHandle, application_offer: str, application_consumer: str
    ) -> list[str]:
        """Find databases that exist in both the offer and consumer database clusters."""
        self.logger.info(f"Finding common databases between '{application_offer}' and '{application_consumer}'")

        # Get databases from both applications (system databases already filtered by client)
        offer_databases = self.database_client.get_databases(model, application_offer)
        consumer_databases = self.database_client.get_databases(model, application_consumer)

        # Find common databases
        common = set(offer_databases) & set(consumer_databases)

        common_list = sorted(common)
        self.logger.info(f"Common databases: {', '.join(common_list) if common_list else 'none'}")
        return common_list
