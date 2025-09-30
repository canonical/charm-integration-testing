# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from abc import ABC, abstractmethod

from juju import JujuBackend


class DatabaseClient(ABC):
    """Abstract base class for database-specific replication operations."""

    @abstractmethod
    def get_databases(self, model: str, application: str) -> list[str]:
        """
        Get list of user databases (excluding system databases).

        Args:
            model: Juju model name
            application: Application name

        Returns:
            List of database names
        """
        raise NotImplementedError

    @abstractmethod
    def get_tables(self, model: str, application: str, database: str) -> list[str]:
        """
        Get list of tables in a database with schema.table format.

        Args:
            model: Juju model name
            application: Application name
            database: Database name

        Returns:
            List of table names in format "schema.table"
        """
        raise NotImplementedError

    @abstractmethod
    def get_replication_config_key(self) -> str:
        """
        Get the configuration key for replication subscription.

        Returns:
            Configuration key name (e.g., 'logical_replication_subscription_request')
        """
        raise NotImplementedError


class PostgresqlDatabaseClient(DatabaseClient):
    """PostgreSQL-specific implementation of DatabaseClient."""

    SYSTEM_DATABASES = {"postgres", "template0", "template1"}
    REPLICATION_CONFIG_KEY = "logical_replication_subscription_request"

    def __init__(self, juju: JujuBackend, logger: logging.Logger):
        self.juju = juju
        self.logger = logger

    def get_databases(self, model: str, application: str) -> list[str]:
        """Get list of databases in a PostgreSQL cluster, excluding system databases."""
        units = self.juju.application_units(model, application)
        if not units:
            return []

        unit = units[0]

        # Query PostgreSQL to list all databases
        # Using psql command to list databases
        command = 'PGPASSWORD=$(cat /var/lib/postgresql/data/pgpass | head -n1 | cut -d: -f5) psql -h localhost -U operator -d postgres -t -c "SELECT datname FROM pg_database WHERE datistemplate = false;"'

        result = self.juju.exec_unit(model, unit, command)

        if result.return_code != 0:
            self.logger.warning(f"Failed to query databases on '{application}': {result.stderr}")
            return []

        # Parse the output - each line is a database name
        databases = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

        # Filter out system databases
        return [db for db in databases if db not in self.SYSTEM_DATABASES]

    def get_tables(self, model: str, application: str, database: str) -> list[str]:
        """Get list of tables in a database with schema prefix (e.g., 'public.users')."""
        units = self.juju.application_units(model, application)
        if not units:
            return []

        unit = units[0]

        # Query for tables in the database, excluding system schemas
        # Database name comes from querying PostgreSQL, not user input
        command = f"""PGPASSWORD=$(cat /var/lib/postgresql/data/pgpass | head -n1 | cut -d: -f5) psql -h localhost -U operator -d {database} -t -c "SELECT schemaname || '.' || tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema');" """  # nosec B608

        result = self.juju.exec_unit(model, unit, command)

        if result.return_code != 0:
            self.logger.warning(f"Failed to query tables in database '{database}' on '{application}': {result.stderr}")
            return []

        # Parse the output - each line is a schema.table name
        tables = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        return tables

    def get_replication_config_key(self) -> str:
        """Get the configuration key for PostgreSQL logical replication subscription."""
        return self.REPLICATION_CONFIG_KEY
