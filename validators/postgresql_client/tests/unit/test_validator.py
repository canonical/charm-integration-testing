# Copyright (C) 2026 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import psycopg2

from validators.postgresql_client.validator import PostgreSQLClientValidator

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class AppStub:
    """Minimal stand-in for ops.Application.  Must be hashable (dict key)."""


class RelationStub:
    def __init__(self, app: AppStub | None, databag: dict[str, str], name: str = "db", id: int = 0) -> None:
        self.app = app
        self.name = name
        self.id = id
        self.data: dict[AppStub | None, dict[str, str]] = {app: databag}


class RelationMetaStub:
    def __init__(self, interface_name: str) -> None:
        self.interface_name = interface_name


class CharmMetaStub:
    def __init__(self, endpoint: str, interface_name: str) -> None:
        self.relations = {endpoint: RelationMetaStub(interface_name)}


class CharmStub:
    def __init__(self, endpoint: str = "db", interface_name: str = "postgresql_client") -> None:
        self.meta = CharmMetaStub(endpoint, interface_name)


def _make_validator(databag: dict[str, str], endpoint: str = "db") -> PostgreSQLClientValidator:
    app = AppStub()
    relation = RelationStub(app=app, databag=databag, name=endpoint)
    charm = cast(ops.CharmBase, CharmStub(endpoint=endpoint))
    return PostgreSQLClientValidator(charm, cast(ops.Relation, relation))


@dataclass
class CursorStub:
    """Minimal cursor context manager; raises execute_error if set."""

    execute_error: Exception | None = None
    # Rows returned by fetchone() for each successive call.
    fetchone_rows: list[tuple[Any, ...]] = field(default_factory=list)
    _call_count: int = field(default=0, init=False, repr=False)

    def execute(self, query: str, params: Any = None) -> None:
        if self.execute_error:
            raise self.execute_error

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._call_count < len(self.fetchone_rows):
            row = self.fetchone_rows[self._call_count]
            self._call_count += 1
            return row
        return None

    def __enter__(self) -> "CursorStub":
        return self

    def __exit__(self, *args: object) -> None:
        pass


@dataclass
class ConnStub:
    """Minimal connection stub; cursor_stub is returned by cursor()."""

    cursor_stub: CursorStub = field(default_factory=CursorStub)
    autocommit: bool = False

    def cursor(self) -> CursorStub:
        return self.cursor_stub

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "uris": "postgresql://myuser:mypassword@10.1.2.3:5432/mydb",
    "database": "mydb",
    "username": "myuser",
    "password": "mypassword",
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPostgreSQLClientValidatorSimple:
    def test_returns_skipped_for_unsupported_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_returns_error_when_relation_app_is_none(self) -> None:
        # GIVEN a relation whose remote app is not yet known
        relation = RelationStub(app=None, databag={})
        validator = PostgreSQLClientValidator(cast(ops.CharmBase, CharmStub()), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"

    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        # GIVEN a databag with all required fields absent
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "uris" in schema_check.message
        assert "database" in schema_check.message
        assert "username" in schema_check.message
        assert "password" in schema_check.message

    def test_passes_with_all_required_fields(self) -> None:
        # GIVEN a complete databag and a successful DB connection
        validator = _make_validator(VALID_DATABAG)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=ConnStub()):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed
        db_check = next(c for c in result.checks if c.name == "database_consistency")
        assert db_check.passed

    def test_fails_database_consistency_when_uri_db_differs(self) -> None:
        # GIVEN a databag where `database` does not match the database in the URI
        databag = {**VALID_DATABAG, "database": "other_db"}
        validator = _make_validator(databag)

        # WHEN (no connect mock needed — should fail before connecting)
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        db_check = next(c for c in result.checks if c.name == "database_consistency")
        assert not db_check.passed
        assert "mydb" in db_check.message
        assert "other_db" in db_check.message

    def test_passes_database_consistency_when_uri_db_is_percent_encoded(self) -> None:
        # GIVEN a URI with a percent-encoded database name that matches the decoded databag field
        databag = {
            "uris": "postgresql://myuser:mypassword@10.1.2.3:5432/my%20db",
            "database": "my db",
            "username": "myuser",
            "password": "mypassword",
        }
        validator = _make_validator(databag)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=ConnStub()):
            result = validator.validate(level="simple")

        assert result.status == "PASS"
        db_check = next(c for c in result.checks if c.name == "database_consistency")
        assert db_check.passed

    def test_fails_connect_check_when_db_unreachable(self) -> None:
        # GIVEN a complete databag but a DB that refuses connections
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.postgresql_client.validator.psycopg2.connect",
            side_effect=psycopg2.OperationalError("Connection refused"),
        ):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "Connection refused" in connect_check.message

    def test_fails_query_check_when_select_raises(self) -> None:
        # GIVEN a connection that succeeds but the canary query raises
        validator = _make_validator(VALID_DATABAG)
        conn = ConnStub(cursor_stub=CursorStub(execute_error=psycopg2.DatabaseError("query error")))

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        query_check = next(c for c in result.checks if c.name == "query")
        assert not query_check.passed

    def test_passes_extensions_check_when_all_installed(self) -> None:
        # GIVEN a databag with extensions and the DB reports them installed
        databag = {**VALID_DATABAG, "extensions": "pg_trgm,hstore"}
        validator = _make_validator(databag)
        # fetchone rows: SELECT 1 (no fetchone), then COUNT(*) = 1 for each extension
        cursor = CursorStub(fetchone_rows=[(1,), (1,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            result = validator.validate(level="simple")

        assert result.status == "PASS"
        ext_check = next(c for c in result.checks if c.name == "extensions")
        assert ext_check.passed
        assert ext_check.message == "OK"

    def test_fails_extensions_check_when_extension_missing(self) -> None:
        # GIVEN pg_trgm is installed but hstore is not
        databag = {**VALID_DATABAG, "extensions": "pg_trgm,hstore"}
        validator = _make_validator(databag)
        cursor = CursorStub(fetchone_rows=[(1,), (0,)])  # pg_trgm present, hstore absent
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            result = validator.validate(level="simple")

        assert result.status == "FAIL"
        ext_check = next(c for c in result.checks if c.name == "extensions")
        assert not ext_check.passed
        assert "hstore" in ext_check.message

    def test_skips_extensions_check_when_field_absent(self) -> None:
        # GIVEN a databag with no extensions field
        validator = _make_validator(VALID_DATABAG)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=ConnStub()):
            result = validator.validate(level="simple")

        assert result.status == "PASS"
        assert not any(c.name == "extensions" for c in result.checks)

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-endpoint")

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=ConnStub()):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-endpoint"
        assert result.interface == "postgresql_client"


class TestPostgreSQLClientValidatorDeep:
    def test_returns_skipped_for_uat_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_returns_error_when_relation_app_is_none(self) -> None:
        # GIVEN a relation whose remote app is not yet known
        relation = RelationStub(app=None, databag={})
        validator = PostgreSQLClientValidator(cast(ops.CharmBase, CharmStub()), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "ERROR"

    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        # GIVEN a databag missing required fields
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed

    def test_fails_connect_check_when_db_unreachable(self) -> None:
        # GIVEN a complete databag but a DB that refuses connections
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.postgresql_client.validator.psycopg2.connect",
            side_effect=psycopg2.OperationalError("Connection refused"),
        ):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "Connection refused" in connect_check.message

    def test_deep_fails_database_consistency_when_uri_db_differs(self) -> None:
        # GIVEN a databag where `database` does not match the database in the URI
        databag = {**VALID_DATABAG, "database": "other_db"}
        validator = _make_validator(databag)

        # WHEN (no connect mock needed — should fail before connecting)
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        db_check = next(c for c in result.checks if c.name == "database_consistency")
        assert not db_check.passed
        assert "mydb" in db_check.message
        assert "other_db" in db_check.message

    def test_deep_passes_on_successful_write_read_verify(self) -> None:
        # GIVEN a complete databag and a connection where INSERT returns an ID
        # and SELECT returns the expected row.
        validator = _make_validator(VALID_DATABAG)
        cursor = CursorStub(fetchone_rows=[(42,), ("validator-probe",)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        assert result.level == "deep"
        write_check = next(c for c in result.checks if c.name == "write_read_verify")
        assert write_check.passed
        cleanup_check = next(c for c in result.checks if c.name == "cleanup")
        assert cleanup_check.passed
        latency_check = next(c for c in result.checks if c.name == "latency")
        assert latency_check.passed

    def test_deep_fails_when_insert_raises(self) -> None:
        # GIVEN a connection that fails on INSERT
        validator = _make_validator(VALID_DATABAG)
        cursor = CursorStub(execute_error=psycopg2.DatabaseError("write error"))
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        write_check = next(c for c in result.checks if c.name == "write_read_verify")
        assert not write_check.passed
        assert "write error" in write_check.message

    def test_deep_fails_when_read_verify_returns_wrong_value(self) -> None:
        # GIVEN INSERT succeeds but SELECT returns a row with the wrong marker
        validator = _make_validator(VALID_DATABAG)
        cursor = CursorStub(fetchone_rows=[(42,), ("wrong-value",)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        write_check = next(c for c in result.checks if c.name == "write_read_verify")
        assert not write_check.passed
        assert "Failed to verify" in write_check.message

    def test_deep_fails_when_insert_returns_no_id(self) -> None:
        # GIVEN INSERT succeeds but fetchone() returns None (no RETURNING row)
        validator = _make_validator(VALID_DATABAG)
        cursor = CursorStub(fetchone_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        write_check = next(c for c in result.checks if c.name == "write_read_verify")
        assert not write_check.passed
        assert "no ID" in write_check.message
