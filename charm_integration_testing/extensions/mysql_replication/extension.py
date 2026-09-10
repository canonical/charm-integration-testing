# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from abc import ABC

from juju import JujuBackend, JujuExtension, JujuModelHandle

from .replicator import CharmInfo, MysqlReplicator


class GenericMysqlDatabaseReplicationExtension(JujuExtension, ABC):
    mysql_replicator: MysqlReplicator

    def __init__(self, mysql_replicator: MysqlReplicator) -> None:
        self.mysql_replicator = mysql_replicator

    def post_deploy(self, model: JujuModelHandle) -> None:
        self.mysql_replicator.try_replicate_all_database_clusters(model)


class MysqlK8sDatabaseReplicationExtension(GenericMysqlDatabaseReplicationExtension):
    def __init__(self, juju: JujuBackend, logger: logging.Logger):
        super().__init__(
            MysqlReplicator(
                CharmInfo(
                    name="mysql-k8s",
                    offer_endpoint="replication-offer",
                    consumer_endpoint="replication",
                ),
                juju,
                logger,
            )
        )


class MysqlDatabaseReplicationExtension(GenericMysqlDatabaseReplicationExtension):
    def __init__(self, juju: JujuBackend, logger: logging.Logger):
        super().__init__(
            MysqlReplicator(
                CharmInfo(
                    name="mysql",
                    offer_endpoint="replication-offer",
                    consumer_endpoint="replication",
                ),
                juju,
                logger,
            )
        )
