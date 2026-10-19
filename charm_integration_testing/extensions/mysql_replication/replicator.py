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
    # Workload status message the offer side's leader unit displays once ready to link (checked
    # via wait_for_unit_message; the application status mirrors the same message but isn't checked).
    create_replication_message: str = "Ready to create replication"


class MysqlReplicator:
    juju: JujuBackend
    logger: logging.Logger

    def __init__(self, charm_info: CharmInfo, juju: JujuBackend, logger: logging.Logger):
        self.juju = juju
        self.logger = logger
        self.charm_info = charm_info

    def try_replicate_all_database_clusters(self, model: JujuModelHandle) -> None:
        applications = self._charm_applications(model)
        if not applications:
            return

        self._try_replicate_same_model_pairs(model, applications)
        self._try_replicate_cross_model_pairs(model, applications)

    def _charm_applications(self, model: JujuModelHandle) -> set[str]:
        """Return matching-charm applications in one model."""
        applications = self.juju.list_applications(model)
        return {name for name, info in applications.items() if info.charm == self.charm_info.name}

    def _try_replicate_same_model_pairs(self, model: JujuModelHandle, applications: set[str]) -> None:
        for application1 in applications:
            for application2 in applications:
                if application1 != application2 and self.juju.integration_exists(
                    application1,
                    self.charm_info.offer_endpoint,
                    application2,
                    self.charm_info.consumer_endpoint,
                    model,
                ):
                    self.logger.info(
                        f"Found replication integration between '{application1}' and '{application2}' "
                        f"in model '{model.uri}'."
                    )
                    self.try_create_replication(model, application1, model, application2)

    def _try_replicate_cross_model_pairs(
        self, consumer_model: JujuModelHandle, consumer_applications: set[str]
    ) -> None:
        for offer_alias, offer_info in self.juju.list_consumed_offers(consumer_model).items():
            if self.charm_info.offer_endpoint not in offer_info.endpoints:
                continue

            parsed_offer_url = offer_info.parse_url()
            if parsed_offer_url is None:
                continue
            offer_model = parsed_offer_url.model
            offer_applications = self._charm_applications(offer_model)
            if len(offer_applications) != 1:
                self.logger.info(
                    f"Skipping cross-model replication for offer '{offer_alias}' in model '{offer_model.uri}': "
                    f"found {len(offer_applications)} '{self.charm_info.name}' applications."
                )
                continue
            (offer_application,) = offer_applications

            for consumer_application in consumer_applications:
                if self.juju.integration_exists(
                    consumer_application,
                    self.charm_info.consumer_endpoint,
                    offer_alias,
                    self.charm_info.offer_endpoint,
                    consumer_model,
                ):
                    self.logger.info(
                        f"Found cross-model replication integration between '{offer_application}' "
                        f"(model '{offer_model.uri}') and '{consumer_application}' "
                        f"(model '{consumer_model.uri}')."
                    )
                    self.try_create_replication(offer_model, offer_application, consumer_model, consumer_application)

    def try_create_replication(
        self,
        offer_model: JujuModelHandle,
        offer_application: str,
        consumer_model: JujuModelHandle,
        consumer_application: str,
    ) -> None:
        # Wait for offer application to be scaled
        self.logger.info(
            f"Waiting for database charm '{self.charm_info.name}' application '{offer_application}' to be scaled"
        )
        self.juju.wait_application_scaled(offer_model, offer_application, timedelta(minutes=10))

        # Wait for offer application units to settle
        self.logger.info(
            f"Waiting for database charm '{self.charm_info.name}' application '{offer_application}' units to be settled"
        )
        self.juju.wait_application_settled(offer_model, offer_application, timedelta(minutes=10))

        # Skip if no units
        if self.juju.num_units(offer_model, offer_application) == 0:
            self.logger.info(f"Skipping replication setup as no units for {offer_application} were found.")
            return

        # Wait for consumer application to be scaled. Unlike the offer side, the consumer's
        # workload legitimately stays in a non-"settled" status (e.g. maintenance: "Setting up
        # replication") until create-replication runs on the offer side, so we can't also wait
        # for it to reach wait_application_settled()'s blocked/active - that would deadlock,
        # since that transition depends on the action this method is about to run.
        self.logger.info(
            f"Waiting for database charm '{self.charm_info.name}' application '{consumer_application}' to be scaled"
        )
        self.juju.wait_application_scaled(consumer_model, consumer_application, timedelta(minutes=10))

        # Skip if consumer has no units
        if self.juju.num_units(consumer_model, consumer_application) == 0:
            self.logger.info(f"Skipping replication setup as no units for {consumer_application} were found.")
            return

        leader_unit = f"{offer_application}/leader"

        if not self._offer_awaiting_replication_setup(offer_model, leader_unit):
            # Either replication was already created by a previous call, or the offer side isn't
            # ready yet for some other reason (e.g. still forming its cluster). Either way there's
            # nothing to do right now; a later post_deploy call will retry if it's still pending.
            self.logger.info(f"'{offer_application}' is not awaiting replication setup, skipping.")
            return

        self.logger.info(f"Creating replication between '{offer_application}' and '{consumer_application}'.")
        self.juju.run_action(offer_model, leader_unit, "create-replication", {})

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
