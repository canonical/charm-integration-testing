# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import re
import ssl
import urllib.parse
import uuid
from dataclasses import dataclass, field

from pyhive import hive  # type: ignore[import-untyped]
from thrift.transport import TSocket, TSSLSocket  # type: ignore[import-untyped]
from thrift_sasl import TSaslClientTransport  # type: ignore[import-untyped]

from validators.base import (
    BasePersistenceValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationCheck,
    ValidationResult,
)

_CANARY_TABLE_PREFIX = "validator_canary_"
_MAX_CANARY_IDENTIFIER = (1 << 63) - 1
_MAX_CHECKPOINT_REF = (1 << 63) - 1
_DEFAULT_PORT = 10009
_SOCKET_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class _ConnectionConfig:
    host: str
    port: int
    database: str
    username: str
    password: str = field(repr=False)
    tls_ca: str | None = None


class _TLSSocket(TSSLSocket.TSSLSocket):  # type: ignore[misc]
    def close(self) -> None:
        # Thrift's SSL socket cannot close an unopened socket after a failed handshake.
        if self.handle is not None:
            super().close()


def _quote_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


class _IncompleteConnectionConfig(RuntimeError):
    pass


class KyuubiClientPersistenceValidator(BasePersistenceValidator):
    """Verify Kyuubi-backed table data survives disruptions for a kyuubi_client relation."""

    def prepare(self) -> PersistenceState:
        self._require_requires_role()
        identifier = uuid.uuid4().int & _MAX_CANARY_IDENTIFIER
        table_name = self._canary_table_name(identifier)
        token = uuid.uuid4().hex
        table = _quote_identifier(table_name)

        conn = self._open_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {table}")  # nosec B608 - generated, quoted identifier
                cur.execute(f"CREATE TABLE {table} (marker STRING NOT NULL, checkpoint_ref BIGINT NOT NULL)")  # nosec B608
                cur.execute(
                    f"INSERT INTO TABLE {table} (marker, checkpoint_ref) VALUES (%s, %s)",  # nosec B608
                    (token, 1),
                )
        finally:
            conn.close()

        return PersistenceState(id=identifier, ref=1, token=token)

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        self._require_requires_role()
        if not 1 <= expected.ref < _MAX_CHECKPOINT_REF:
            raise ValueError(f"expected.ref {expected.ref} is out of range (expected 1..{_MAX_CHECKPOINT_REF - 1})")
        if not expected.token:
            raise ValueError("expected.token must not be empty")

        table_name = self._canary_table_name(expected.id)
        table = _quote_identifier(table_name)
        conn = self._open_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES")
                tables = cur.fetchall()
                table_exists = any(table_name in {str(value) for value in row} for row in tables)
                if table_exists:
                    cur.execute(
                        f"SELECT COUNT(*), "  # nosec B608 - generated, quoted identifier
                        "COUNT(DISTINCT CASE WHEN checkpoint_ref BETWEEN 1 AND %s THEN checkpoint_ref END) "
                        f"FROM {table} WHERE marker = %s",
                        (expected.ref, expected.token),
                    )
                    row = cur.fetchone()
                    matching = int(row[0]) if row else 0
                    distinct_refs = int(row[1]) if row else 0
                else:
                    matching = 0
                    distinct_refs = 0

                passed = matching == distinct_refs == expected.ref
                if passed:
                    next_ref = expected.ref + 1
                    cur.execute(
                        f"INSERT INTO TABLE {table} (marker, checkpoint_ref) VALUES (%s, %s)",  # nosec B608
                        (expected.token, next_ref),
                    )
        finally:
            conn.close()

        check = ValidationCheck(
            name="row_count",
            passed=passed,
            message=(
                f"Found {matching} canary row(s) with references 1..{expected.ref} in '{table_name}'."
                if passed
                else (
                    f"Expected {expected.ref} canary row(s) with references 1..{expected.ref} in '{table_name}', "
                    f"found {matching} row(s) and {distinct_refs} distinct in-range reference(s)."
                )
            ),
        )
        result = self._make_result(level="deep", checks=[check])
        new_state = PersistenceState(id=expected.id, ref=expected.ref + 1, token=expected.token) if passed else expected
        return result, new_state

    def cleanup(self) -> None:
        self._require_requires_role()
        try:
            self._connection_config()
        except _IncompleteConnectionConfig as error:
            raise PersistenceNotApplicable(
                "Relation connection fields are incomplete; cleanup cannot remove canary data yet."
            ) from error

        conn = self._open_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES")
                tables = cur.fetchall()

            name_regex = re.compile(re.escape(self._canary_table_prefix()) + r"(?P<identifier>[0-9]{20})")
            for row in tables:
                table_name = next((str(value) for value in row if name_regex.fullmatch(str(value))), None)
                if table_name is None:
                    continue
                match = name_regex.fullmatch(table_name)
                if match is None or int(match.group("identifier")) > _MAX_CANARY_IDENTIFIER:
                    continue
                with conn.cursor() as cur:
                    cur.execute(f"DROP TABLE IF EXISTS {_quote_identifier(table_name)}")  # nosec B608
        finally:
            conn.close()

    def _require_requires_role(self) -> None:
        if self.role != "requires":
            raise PersistenceNotApplicable(f"Role '{self.role}' is not supported by {self.__class__.__name__}.")

    def _connection_config(self) -> _ConnectionConfig:
        credentials = {
            **self.resolve_secret("secret-user", "username", "password", "uris", "endpoints", "database"),
            **self.resolve_secret("secret-tls", "tls", "tls-ca"),
        }
        data = self.databag | credentials
        target = data.get("uris") or data.get("endpoints", "")
        if not target:
            raise _IncompleteConnectionConfig("Cannot connect to Kyuubi: missing relation field 'uris' or 'endpoints'.")

        address = target.split(",")[0].strip()
        if address.startswith("jdbc:"):
            address = address[len("jdbc:") :]
        parsed = urllib.parse.urlsplit(address if "://" in address else f"//{address}")
        if parsed.scheme and parsed.scheme.lower() not in {"kyuubi", "hive2"}:
            raise _IncompleteConnectionConfig(f"Cannot connect to Kyuubi: unsupported URI scheme '{parsed.scheme}'.")
        try:
            host = parsed.hostname
            port = parsed.port or _DEFAULT_PORT
        except ValueError as error:
            raise _IncompleteConnectionConfig("Cannot connect to Kyuubi: invalid endpoint URI.") from error
        if not host:
            raise _IncompleteConnectionConfig("Cannot connect to Kyuubi: endpoint URI has no host.")

        uri_database = urllib.parse.unquote(parsed.path.strip("/"))
        database = data.get("database") or uri_database or "default"
        if uri_database and data.get("database") and uri_database != data["database"]:
            raise _IncompleteConnectionConfig("Kyuubi URI database does not match the relation 'database' field.")

        username = data.get("username", "")
        password = data.get("password", "")
        if not username.strip() or not password.strip():
            raise _IncompleteConnectionConfig(
                "Cannot connect to Kyuubi: usable username and password are required; "
                "LDAP placeholders do not supply login credentials."
            )

        tls = data.get("tls", "false").strip().lower()
        if tls not in {"true", "false"}:
            raise _IncompleteConnectionConfig("Cannot connect to Kyuubi: 'tls' must be True or False.")
        tls_ca = data.get("tls-ca", "") if tls == "true" else None
        if tls == "true" and not (tls_ca and tls_ca.strip()):
            raise _IncompleteConnectionConfig("Cannot connect to Kyuubi: TLS is enabled but 'tls-ca' is missing.")

        return _ConnectionConfig(host, port, database, username, password, tls_ca)

    def _open_connection(self) -> "hive.Connection":
        config = self._connection_config()
        if config.tls_ca is not None:
            context = ssl.create_default_context(cadata=config.tls_ca)
            socket = _TLSSocket(config.host, config.port, ssl_context=context)
        else:
            socket = TSocket.TSocket(config.host, config.port)
        socket.setTimeout(_SOCKET_TIMEOUT_SECONDS * 1000)
        transport = TSaslClientTransport(
            lambda: hive.get_installed_sasl(
                host=config.host,
                sasl_auth="PLAIN",
                username=config.username,
                password=config.password,
            ),
            "PLAIN",
            socket,
        )
        return hive.Connection(
            database=config.database,
            username=config.username,
            thrift_transport=transport,
        )

    def _canary_table_prefix(self) -> str:
        scope = f"{self.charm.model.uuid}:{self.relation_id}:{self.charm.model.unit.name}"
        scope_token = hashlib.sha256(scope.encode()).hexdigest()[:16]
        return f"{_CANARY_TABLE_PREFIX}{scope_token}_"

    def _canary_table_name(self, identifier: int) -> str:
        if not 0 <= identifier <= _MAX_CANARY_IDENTIFIER:
            raise ValueError(f"canary identifier {identifier} is out of range (expected 0..{_MAX_CANARY_IDENTIFIER})")
        return f"{self._canary_table_prefix()}{identifier:020d}"
