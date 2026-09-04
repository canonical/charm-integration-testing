# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta

from juju import JujuBackend, JujuModelHandle, JujuWaitTimeoutError
from pydantic.dataclasses import dataclass


@dataclass
class CharmInfo:
    name: str
    offer_endpoint: str
    consumer_endpoint: str
    # App status message shown on the offer side once both clusters are ready to be linked.
    create_replication_message: str = "Ready to create replication"


class MysqlReplicator:
    juju: JujuBackend
    logger: logging.Logger

    def __init__(self, charm_info: CharmInfo, juju: JujuBackend, logger: logging.Logger):
        self.juju = juju
        self.logger = logger
        self.charm_info = charm_info

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
                        self.try_create_replication(
                            model=model, application_offer=application1, application_consumer=application2
                        )

    def try_create_replication(self, model: JujuModelHandle, application_offer: str, application_consumer: str) -> None:
        # Wait for offer application to be scaled
        self.logger.info(
            f"Waiting for database charm '{self.charm_info.name}' application '{application_offer}' to be scaled"
        )
        self.juju.wait_application_scaled(model, application_offer, timedelta(minutes=10))

        # Wait for offer application units to settle
        self.logger.info(
            f"Waiting for database charm '{self.charm_info.name}' application '{application_offer}' units to be settled"
        )
        self.juju.wait_application_settled(model, application_offer, timedelta(minutes=10))

        # Skip if no units
        if self.juju.num_units(model, application_offer) == 0:
            self.logger.info(f"Skipping replication setup as no units for {application_offer} were found.")
            return

        # Wait for consumer application to be scaled. Unlike the offer side, the consumer's
        # workload legitimately stays in a non-"settled" status (e.g. maintenance: "Setting up
        # replication") until create-replication runs on the offer side, so we can't also wait
        # for it to reach wait_application_settled()'s blocked/active - that would deadlock,
        # since that transition depends on the action this method is about to run.
        self.logger.info(
            f"Waiting for database charm '{self.charm_info.name}' application '{application_consumer}' to be scaled"
        )
        self.juju.wait_application_scaled(model, application_consumer, timedelta(minutes=10))

        # Skip if consumer has no units
        if self.juju.num_units(model, application_consumer) == 0:
            self.logger.info(f"Skipping replication setup as no units for {application_consumer} were found.")
            return

        leader_unit = f"{application_offer}/leader"

        if not self._offer_awaiting_replication_setup(model, leader_unit):
            # Either replication was already created by a previous call, or the offer side isn't
            # ready yet for some other reason (e.g. still forming its cluster). Either way there's
            # nothing to do right now; a later post_deploy call will retry if it's still pending.
            self.logger.info(f"'{application_offer}' is not awaiting replication setup, skipping.")
            return

        self.logger.info(f"Creating replication between '{application_offer}' and '{application_consumer}'.")
        self.juju.run_action(model, leader_unit, "create-replication", {})

    def _offer_awaiting_replication_setup(self, model: JujuModelHandle, unit: str) -> bool:
        """Cheaply check whether ``unit`` is currently displaying the create-replication message.

        Used to distinguish "still needs replication set up" from "already done" without blocking
        for the full wait_for_unit_message timeout in the (common, already-done) latter case.
        """
        try:
            self.juju.wait_for_unit_message(
                model, unit, self.charm_info.create_replication_message, timedelta(seconds=5)
            )
            return True
        except JujuWaitTimeoutError:
            return False
