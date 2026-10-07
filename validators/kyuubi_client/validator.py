# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import re
import urllib.parse
import uuid

from pyhive import hive  # type: ignore[import-untyped]

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
_DEFAULT_USERNAME = "validator"


def _quote_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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
                    f"INSERT INTO TABLE {table} (marker, checkpoint_ref) " f"VALUES ({_quote_literal(token)}, 1)"  # nosec B608 - generated and escaped token
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
        token = _quote_literal(expected.token)
        conn = self._open_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES")
                tables = cur.fetchall()
                table_exists = any(table_name in {str(value) for value in row} for row in tables)
                if table_exists:
                    cur.execute(
                        f"SELECT COUNT(*) FROM {table} "  # nosec B608 - generated, quoted identifier
                        f"WHERE marker = {token}"  # nosec B608 - token is SQL-literal escaped
                    )
                    row = cur.fetchone()
                    matching = int(row[0]) if row else 0
                else:
                    matching = 0

                passed = matching == expected.ref
                if passed:
                    next_ref = expected.ref + 1
                    cur.execute(
                        f"INSERT INTO TABLE {table} (marker, checkpoint_ref) "  # nosec B608
                        f"VALUES ({token}, {next_ref})"  # nosec B608 - token is SQL-literal escaped
                    )
        finally:
            conn.close()

        check = ValidationCheck(
            name="row_count",
            passed=passed,
            message=(
                f"Found expected {matching} canary row(s) in '{table_name}'."
                if passed
                else f"Expected {expected.ref} canary row(s) with matching identity in '{table_name}', found {matching}."
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

    def _connection_config(self) -> dict[str, str | int]:
        credentials = {
            **self.resolve_secret("secret-user", "username", "password", "uris", "endpoints", "database"),
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
        username = username if username.strip() else ""
        password = password if password.strip() else ""
        if bool(username) != bool(password):
            raise _IncompleteConnectionConfig(
                "Cannot connect to Kyuubi: username and password must be provided together."
            )

        return {
            "host": host,
            "port": port,
            "database": database,
            "username": username or _DEFAULT_USERNAME,
            "password": password,
            "auth": "LDAP" if password else "NONE",
        }

    def _open_connection(self) -> "hive.Connection":
        config = self._connection_config()
        return hive.Connection(
            host=str(config["host"]),
            port=int(config["port"]),
            database=str(config["database"]),
            username=str(config["username"]),
            password=str(config["password"]) if config["auth"] == "LDAP" else None,
            auth=str(config["auth"]),
        )

    def _canary_table_prefix(self) -> str:
        scope = f"{self.charm.model.uuid}:{self.relation_id}:{self.charm.model.unit.name}"
        scope_token = hashlib.sha256(scope.encode()).hexdigest()[:16]
        return f"{_CANARY_TABLE_PREFIX}{scope_token}_"

    def _canary_table_name(self, identifier: int) -> str:
        if not 0 <= identifier <= _MAX_CANARY_IDENTIFIER:
            raise ValueError(f"canary identifier {identifier} is out of range (expected 0..{_MAX_CANARY_IDENTIFIER})")
        return f"{self._canary_table_prefix()}{identifier:020d}"
