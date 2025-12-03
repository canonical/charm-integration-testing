# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from extensions.database_replication.database_client import DatabaseClient
from extensions.database_replication.database_replicator import CharmInfo
from extensions.database_replication.extension import GenericDatabaseReplicationExtension
from juju.backend import JujuBackend


@dataclass
class DatabaseClientStub(DatabaseClient):
    """Stub implementation of DatabaseClient for testing"""

    databases_by_app: dict[str, list[str]] = field(default_factory=lambda: {"postgresql-1": ["testdb"], "postgresql-2": ["testdb"]})
    tables_by_db: dict[str, list[str]] = field(default_factory=lambda: {"testdb": ["public.users", "public.orders"]})

    def get_databases(self, model: str, application: str) -> list[str]:
        """Return databases for an application"""
        return self.databases_by_app.get(application, [])

    def get_tables(self, model: str, application: str, database: str) -> list[str]:
        """Return tables for a database"""
        return self.tables_by_db.get(database, [])

    def get_replication_config_key(self) -> str:
        """Return the config key for replication"""
        return "logical_replication_subscription_request"


@dataclass
class JujuStub:
    """Stub implementation of JujuBackend for testing DatabaseReplicator"""

    applications: dict[str, str] = field(default_factory=lambda: {"postgresql-1": "postgresql", "postgresql-2": "postgresql"})
    integrations: list = field(default_factory=list)
    waited_scaled: list = field(default_factory=list)
    waited_settled: list = field(default_factory=list)
    units: dict[str, int] = field(default_factory=lambda: {"postgresql-1": 3, "postgresql-2": 3})
    configured_applications: list = field(default_factory=list)

    def list_applications(self, model: str) -> list[str]:
        """Return list of application names in the model"""
        return list(self.applications.keys())

    def application_charm(self, model: str, application: str) -> str:
        """Return the charm name for a given application"""
        return self.applications[application]

    def integration_exists(
        self, application1: str, endpoint1: str, application2: str, endpoint2: str, model: str
    ) -> bool:
        """Check if an integration exists between two applications"""
        return (application1, endpoint1, application2, endpoint2) in self.integrations

    def wait_application_scaled(self, model: str, application: str, timeout: timedelta) -> None:
        """Wait for application to be scaled (captures call for verification)"""
        self.waited_scaled.append((model, application, str(timeout)))

    def wait_application_settled(self, model: str, application: str, timeout: timedelta) -> None:
        """Wait for application to settle (captures call for verification)"""
        self.waited_settled.append((model, application, str(timeout)))

    def num_units(self, model: str, application: str) -> int:
        """Return the number of units for an application"""
        return self.units.get(application, 0)

    def configure_application(self, model: str, application: str, values: dict) -> None:
        """Mock configuring an application (captures call for verification)"""
        self.configured_applications.append((model, application, values))


class TestPostgreSQLDatabaseReplicationExtension:
    @pytest.fixture
    def juju(self) -> JujuStub:
        """Provide a JujuStub instance for testing"""
        return JujuStub()

    @pytest.fixture
    def database_client(self) -> DatabaseClientStub:
        """Provide a DatabaseClientStub instance for testing"""
        return DatabaseClientStub()

    @pytest.fixture
    def extension(self, juju: JujuStub, database_client: DatabaseClientStub) -> GenericDatabaseReplicationExtension:
        """Provide a PostgresqlDatabaseReplicationExtension instance with stubbed client"""
        from extensions.database_replication.database_replicator import DatabaseReplicator

        charm_info = CharmInfo(
            name="postgresql", offer_endpoint="logical-replication-offer", consumer_endpoint="logical-replication"
        )
        replicator = DatabaseReplicator(charm_info, juju, logging.getLogger("test"), database_client)

        ext = GenericDatabaseReplicationExtension(replicator)
        return ext

    class TestPostDeploy:
        def test_skips_when_no_postgresql_applications(
            self, juju: JujuStub, database_client: DatabaseClientStub
        ) -> None:
            # GIVEN a model with no postgresql applications
            juju.applications = {"other-app": "other-charm"}
            from extensions.database_replication.database_replicator import DatabaseReplicator
            from extensions.database_replication.extension import GenericDatabaseReplicationExtension

            charm_info = CharmInfo(
                name="postgresql", offer_endpoint="logical-replication-offer", consumer_endpoint="logical-replication"
            )
            replicator = DatabaseReplicator(charm_info, juju, logging.getLogger("test"), database_client)
            extension = GenericDatabaseReplicationExtension(replicator)

            # WHEN post_deploy is called
            extension.post_deploy("test-model")

            # THEN no operations are performed
            assert juju.waited_scaled == []
            assert juju.waited_settled == []

        def test_skips_when_only_one_postgresql_application(
            self, juju: JujuStub, database_client: DatabaseClientStub
        ) -> None:
            # GIVEN a model with only one postgresql application
            juju.applications = {"postgresql-1": "postgresql"}
            from extensions.database_replication.database_replicator import DatabaseReplicator
            from extensions.database_replication.extension import GenericDatabaseReplicationExtension

            charm_info = CharmInfo(
                name="postgresql", offer_endpoint="logical-replication-offer", consumer_endpoint="logical-replication"
            )
            replicator = DatabaseReplicator(charm_info, juju, logging.getLogger("test"), database_client)
            extension = GenericDatabaseReplicationExtension(replicator)

            # WHEN post_deploy is called
            extension.post_deploy("test-model")

            # THEN no operations are performed
            assert juju.waited_scaled == []
            assert juju.waited_settled == []

        def test_processes_when_two_or_more_postgresql_applications_with_integration(
            self, extension: GenericDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with 2+ postgresql applications and an integration
            juju.integrations = [("postgresql-1", "logical-replication-offer", "postgresql-2", "logical-replication")]

            # WHEN post_deploy is called
            extension.post_deploy("test-model")

            # THEN applications are waited on for scaling and settling
            assert ("test-model", "postgresql-1", "0:10:00") in juju.waited_scaled
            assert ("test-model", "postgresql-1", "0:10:00") in juju.waited_settled

        def test_skips_when_no_integrations_exist(
            self, extension: GenericDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with 2+ postgresql applications but no integrations
            juju.integrations = []

            # WHEN post_deploy is called
            extension.post_deploy("test-model")

            # THEN no operations are performed
            assert juju.waited_scaled == []
            assert juju.waited_settled == []

    class TestTryReplicateDatabaseCluster:
        def test_waits_for_application_to_scale_and_settle(
            self, extension: GenericDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with postgresql applications
            database_replicator = extension.database_replicator

            # WHEN try_replicate_database_cluster is called
            database_replicator.try_replicate_database_cluster(
                model="test-model", application_offer="postgresql-1", application_consumer="postgresql-2"
            )

            # THEN application is waited on
            assert ("test-model", "postgresql-1", "0:10:00") in juju.waited_scaled
            assert ("test-model", "postgresql-1", "0:10:00") in juju.waited_settled

        def test_skips_when_no_units_exist(
            self, extension: GenericDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN an application with no units
            juju.units["postgresql-1"] = 0
            database_replicator = extension.database_replicator

            # WHEN try_replicate_database_cluster is called
            database_replicator.try_replicate_database_cluster(
                model="test-model", application_offer="postgresql-1", application_consumer="postgresql-2"
            )

            # THEN waits still occur but no further operations
            assert len(juju.waited_scaled) > 0
            assert len(juju.waited_settled) > 0
            # No configuration should happen
            assert len(juju.configured_applications) == 0

        def test_configures_replication_when_common_databases_and_tables_exist(
            self, extension: GenericDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with postgresql applications that have common databases and tables
            juju.integrations = [("postgresql-1", "logical-replication-offer", "postgresql-2", "logical-replication")]
            database_replicator = extension.database_replicator

            # WHEN try_replicate_database_cluster is called
            database_replicator.try_replicate_database_cluster(
                model="test-model", application_offer="postgresql-1", application_consumer="postgresql-2"
            )

            # THEN both applications are waited on
            assert ("test-model", "postgresql-1", "0:10:00") in juju.waited_scaled
            assert ("test-model", "postgresql-1", "0:10:00") in juju.waited_settled
            assert ("test-model", "postgresql-2", "0:10:00") in juju.waited_scaled
            assert ("test-model", "postgresql-2", "0:10:00") in juju.waited_settled

            # AND replication configuration is applied
            assert len(juju.configured_applications) == 1
            model, app, config = juju.configured_applications[0]
            assert model == "test-model"
            assert app == "postgresql-2"
            assert "logical_replication_subscription_request" in config

            # Verify the subscription request contains the expected structure
            import json

            subscription_request = json.loads(config["logical_replication_subscription_request"])
            assert "testdb" in subscription_request
            assert isinstance(subscription_request["testdb"], list)
            assert "public.users" in subscription_request["testdb"]
            assert "public.orders" in subscription_request["testdb"]

        def test_skips_when_consumer_has_no_units(
            self, extension: GenericDatabaseReplicationExtension, juju: JujuStub
        ) -> None:
            # GIVEN an application where consumer has no units
            juju.units["postgresql-2"] = 0
            database_replicator = extension.database_replicator

            # WHEN try_replicate_database_cluster is called
            database_replicator.try_replicate_database_cluster(
                model="test-model", application_offer="postgresql-1", application_consumer="postgresql-2"
            )

            # THEN both are waited on but no configuration happens
            assert ("test-model", "postgresql-1", "0:10:00") in juju.waited_scaled
            assert ("test-model", "postgresql-2", "0:10:00") in juju.waited_scaled
            assert len(juju.configured_applications) == 0

        def test_skips_when_no_common_databases(
            self, extension: GenericDatabaseReplicationExtension, juju: JujuStub, database_client: DatabaseClientStub
        ) -> None:
            # GIVEN applications where database queries return different databases
            database_replicator = extension.database_replicator

            # Mock database client to return different databases for each application
            database_client.databases_by_app = {"postgresql-1": ["db1"], "postgresql-2": ["db2"]}

            # WHEN try_replicate_database_cluster is called
            database_replicator.try_replicate_database_cluster(
                model="test-model", application_offer="postgresql-1", application_consumer="postgresql-2"
            )

            # THEN no configuration is applied
            assert len(juju.configured_applications) == 0

        def test_skips_when_no_tables_in_common_databases(
            self, extension: GenericDatabaseReplicationExtension, juju: JujuStub, database_client: DatabaseClientStub
        ) -> None:
            # GIVEN applications with common databases but no tables
            database_replicator = extension.database_replicator

            # Mock database client to return empty table lists
            database_client.tables_by_db = {"testdb": []}

            # WHEN try_replicate_database_cluster is called
            database_replicator.try_replicate_database_cluster(
                model="test-model", application_offer="postgresql-1", application_consumer="postgresql-2"
            )

            # THEN no configuration is applied
            assert len(juju.configured_applications) == 0
