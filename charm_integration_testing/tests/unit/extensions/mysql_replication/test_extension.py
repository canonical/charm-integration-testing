# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from extensions.mysql_replication.extension import GenericMysqlDatabaseReplicationExtension
from extensions.mysql_replication.replicator import CharmInfo, MysqlReplicator
from juju import JujuModelHandle, JujuWaitTimeoutError
from juju.models import JujuIntegrationApplication

from ..shared import JujuStub as JujuStubBase

TEST_MODEL: JujuModelHandle = JujuModelHandle(controller="test-controller", model="test-model")


@dataclass
class JujuStub(JujuStubBase):
    """Stub implementation of JujuBackend for testing MysqlReplicator"""

    applications: dict[str, str] = field(default_factory=lambda: {"mysql-1": "mysql-k8s", "mysql-2": "mysql-k8s"})
    units: dict[str, int] = field(default_factory=lambda: {"mysql-1": 3, "mysql-2": 3})
    # Optional per-unit "current" workload message. When set for a unit, wait_for_unit_message
    # raises JujuWaitTimeoutError unless the requested message matches, simulating a real charm
    # that has moved past (or not yet reached) that message. Units not present here always
    # succeed immediately, preserving existing tests' assumptions.
    unit_messages: dict[str, str] = field(default_factory=dict)

    def num_units(self, model: JujuModelHandle, application: str) -> int:
        """Return the number of units for an application"""
        return self.units.get(application, 0)

    def wait_for_unit_message(self, model: JujuModelHandle, unit: str, message: str, timeout: timedelta | None) -> None:
        super().wait_for_unit_message(model, unit, message, timeout)
        current = self.unit_messages.get(unit)
        if current is not None and message.lower() not in current.lower():
            raise JujuWaitTimeoutError()


@pytest.fixture
def charm_info() -> CharmInfo:
    return CharmInfo(name="mysql-k8s", offer_endpoint="replication-offer", consumer_endpoint="replication")


class TestMysqlDatabaseReplicationExtension:
    @pytest.fixture
    def juju(self) -> JujuStub:
        """Provide a JujuStub instance for testing"""
        return JujuStub()

    @pytest.fixture
    def extension(self, juju: JujuStub, charm_info: CharmInfo) -> GenericMysqlDatabaseReplicationExtension:
        replicator = MysqlReplicator(charm_info, juju, logging.getLogger("test"))
        return GenericMysqlDatabaseReplicationExtension(replicator)

    class TestPostDeploy:
        def test_skips_when_no_mysql_applications(self, juju: JujuStub, charm_info: CharmInfo) -> None:
            # GIVEN a model with no mysql applications
            juju.applications = {"other-app": "other-charm"}
            replicator = MysqlReplicator(charm_info, juju, logging.getLogger("test"))
            extension = GenericMysqlDatabaseReplicationExtension(replicator)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN no operations are performed
            assert juju.waited_scaled == []
            assert juju.actions == []

        def test_skips_when_only_one_mysql_application(self, juju: JujuStub, charm_info: CharmInfo) -> None:
            # GIVEN a model with only one mysql application
            juju.applications = {"mysql-1": "mysql-k8s"}
            replicator = MysqlReplicator(charm_info, juju, logging.getLogger("test"))
            extension = GenericMysqlDatabaseReplicationExtension(replicator)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN no operations are performed
            assert juju.waited_scaled == []
            assert juju.actions == []

        def test_skips_when_no_integrations_exist(
            self, extension: GenericMysqlDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with 2+ mysql applications but no integrations

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN no operations are performed
            assert juju.waited_scaled == []
            assert juju.actions == []

        def test_creates_replication_when_offer_and_consumer_integrated_and_ready(
            self, extension: GenericMysqlDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with 2+ mysql applications related via the replication-offer endpoint
            juju.integrate(
                TEST_MODEL,
                JujuIntegrationApplication("mysql-1", "replication-offer"),
                JujuIntegrationApplication("mysql-2", "replication"),
            )

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN both applications are waited on for scaling, and the offer is waited on to settle
            assert (TEST_MODEL.uri, "mysql-1", "0:10:00") in juju.waited_scaled
            assert (TEST_MODEL.uri, "mysql-1", "0:10:00") in juju.waited_settled
            assert (TEST_MODEL.uri, "mysql-2", "0:10:00") in juju.waited_scaled

            # AND the consumer is NOT waited on to settle: it legitimately stays in a
            # non-blocked/active status (e.g. "Setting up replication") until create-replication
            # runs, so waiting for it to settle first would deadlock.
            assert (TEST_MODEL.uri, "mysql-2", "0:10:00") not in juju.waited_settled

            # AND the create-replication action is run on the offer side's leader unit
            assert (TEST_MODEL.uri, "mysql-1/leader", "create-replication", {}) in juju.actions

    class TestTryCreateReplication:
        def test_skips_when_offer_has_no_units(
            self, extension: GenericMysqlDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN an offer application with no units
            juju.units["mysql-1"] = 0
            replicator = extension.mysql_replicator

            # WHEN try_create_replication is called
            replicator.try_create_replication(
                model=TEST_MODEL, application_offer="mysql-1", application_consumer="mysql-2"
            )

            # THEN no action is run
            assert juju.actions == []

        def test_skips_when_consumer_has_no_units(
            self, extension: GenericMysqlDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN a consumer application with no units
            juju.units["mysql-2"] = 0
            replicator = extension.mysql_replicator

            # WHEN try_create_replication is called
            replicator.try_create_replication(
                model=TEST_MODEL, application_offer="mysql-1", application_consumer="mysql-2"
            )

            # THEN no action is run
            assert juju.actions == []

        def test_skips_when_offer_not_awaiting_replication_message(
            self, extension: GenericMysqlDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN the offer leader unit has already moved past the create-replication message
            # (e.g. replication was already created by a previous post_deploy call)
            juju.unit_messages["mysql-1/leader"] = "active"
            replicator = extension.mysql_replicator

            # WHEN try_create_replication is called
            replicator.try_create_replication(
                model=TEST_MODEL, application_offer="mysql-1", application_consumer="mysql-2"
            )

            # THEN no action is run
            assert juju.actions == []

        def test_runs_create_replication_action_when_offer_is_ready(
            self, extension: GenericMysqlDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN both applications are scaled and settled with units, and the offer side is
            # displaying the "ready to create replication" message

            replicator = extension.mysql_replicator

            # WHEN try_create_replication is called
            replicator.try_create_replication(
                model=TEST_MODEL, application_offer="mysql-1", application_consumer="mysql-2"
            )

            # THEN both applications are waited on, but only the offer is waited on to settle
            assert (TEST_MODEL.uri, "mysql-1", "0:10:00") in juju.waited_scaled
            assert (TEST_MODEL.uri, "mysql-1", "0:10:00") in juju.waited_settled
            assert (TEST_MODEL.uri, "mysql-2", "0:10:00") in juju.waited_scaled
            assert (TEST_MODEL.uri, "mysql-2", "0:10:00") not in juju.waited_settled

            # AND the create-replication action is run on the offer's leader unit
            assert (TEST_MODEL.uri, "mysql-1/leader", "create-replication", {}) in juju.actions
