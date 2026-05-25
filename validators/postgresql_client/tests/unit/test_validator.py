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
from typing import cast
from unittest.mock import patch

import ops
import psycopg2
from test_utils.stubs import RelationRoleStub, AppStub, RelationStub, CharmStub

from validators.postgresql_client.validator import PostgreSQLClientValidator

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _make_validator(databag: dict[str, str], endpoint: str = "db") -> PostgreSQLClientValidator:
    app = AppStub()
    relation = RelationStub(app=app, data={app: databag}, name=endpoint)
    charm = cast(ops.CharmBase, CharmStub(relation_name=endpoint, interface_name="postgresql_client"))
    return PostgreSQLClientValidator(charm, cast(ops.Relation, relation))


@dataclass
class CursorStub:
    """Minimal cursor context manager; raises execute_error if set."""

    execute_error: Exception | None = None

    def execute(self, query: str) -> None:
        if self.execute_error:
            raise self.execute_error

    def __enter__(self) -> "CursorStub":
        return self

    def __exit__(self, *args: object) -> None:
        pass


@dataclass
class ConnStub:
    """Minimal connection stub; cursor_stub is returned by cursor()."""

    cursor_stub: CursorStub = field(default_factory=CursorStub)

    def cursor(self) -> CursorStub:
        return self.cursor_stub

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "endpoints": "10.1.2.3:5432",
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
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_returns_error_when_relation_app_is_none(self) -> None:
        # GIVEN a relation whose remote app is not yet known
        relation = RelationStub(name="test-relation", app=None, data={})
        validator = PostgreSQLClientValidator(cast(ops.CharmBase, CharmStub(relation_name=relation.name)), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"

    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        # GIVEN a databag with missing required fields
        validator = _make_validator({"endpoints": "10.1.2.3:5432"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "database" in schema_check.message
        assert "username" in schema_check.message
        assert "password" in schema_check.message

    def test_passes_schema_check_with_all_required_fields(self) -> None:
        # GIVEN a complete databag and a successful DB connection
        validator = _make_validator(VALID_DATABAG)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=ConnStub()):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed

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

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-endpoint")

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=ConnStub()):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-endpoint"
        assert result.interface == "postgresql_client"
