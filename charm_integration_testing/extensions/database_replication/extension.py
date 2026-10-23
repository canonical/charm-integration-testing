# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from abc import ABC

from juju import JujuBackend, JujuExtension, JujuModelHandle

from .database_client import PostgresqlDatabaseClient
from .database_replicator import CharmInfo, DatabaseReplicator


class GenericDatabaseReplicationExtension(JujuExtension, ABC):
    database_replicator: DatabaseReplicator

    def __init__(self, database_replicator: DatabaseReplicator) -> None:
        self.database_replicator = database_replicator

    def post_deploy(self, model: JujuModelHandle) -> None:
        self.database_replicator.try_replicate_all_database_clusters(model)


class PostgresqlK8sDatabaseReplicationExtension(GenericDatabaseReplicationExtension):
    def __init__(self, juju: JujuBackend, logger: logging.Logger):
        database_client = PostgresqlDatabaseClient(juju, logger)
        super().__init__(
            DatabaseReplicator(
                CharmInfo(
                    name="postgresql-k8s",
                    offer_endpoint="logical-replication-offer",
                    consumer_endpoint="logical-replication",
                ),
                juju,
                logger,
                database_client,
            )
        )


class PostgresqlDatabaseReplicationExtension(GenericDatabaseReplicationExtension):
    def __init__(self, juju: JujuBackend, logger: logging.Logger):
        database_client = PostgresqlDatabaseClient(juju, logger)
        super().__init__(
            DatabaseReplicator(
                CharmInfo(
                    name="postgresql",
                    offer_endpoint="logical-replication-offer",
                    consumer_endpoint="logical-replication",
                ),
                juju,
                logger,
                database_client,
            )
        )
