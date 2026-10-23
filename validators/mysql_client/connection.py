# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pymysql

# Fields both validators need before a connection can be opened.
REQUIRED_CREDENTIAL_FIELDS = ["endpoints", "database", "username", "password"]


class _MySQLConnectionMixin:
    """Shared credential-resolution and connection helpers for mysql_client validators.

    Both ``MySQLClientValidator`` (health probe) and ``MySQLClientPersistenceValidator``
    (durability probe) resolve the same relation credentials and open the same kind of PyMySQL
    connection, so that logic lives here once instead of being duplicated.
    """

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve credentials from the relation databag or Juju secrets."""
        return {
            **self.resolve_secret("secret-user", "username", "password"),  # type: ignore[attr-defined]
            **self.resolve_secret("secret-tls", "tls-ca"),  # type: ignore[attr-defined]
        }

    def _first_endpoint(self, data: dict[str, str]) -> tuple[str, int]:
        """Split the first `endpoints` entry into (host, port)."""
        first = data["endpoints"].split(",")[0].strip()
        host, _, port = first.partition(":")
        return host, int(port) if port else 3306

    def _connect(self, data: dict[str, str]) -> "pymysql.connections.Connection":
        """Open a PyMySQL connection using databag/secret fields."""
        host, port = self._first_endpoint(data)
        return pymysql.connect(
            host=host,
            port=port,
            user=data["username"],
            password=data["password"],
            database=data["database"],
            connect_timeout=5,
        )
