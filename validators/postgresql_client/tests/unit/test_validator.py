# Copyright (C) 2025 Canonical Ltd
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

from unittest.mock import MagicMock, patch

import psycopg2

from validators.postgresql_client.validator import PostgreSQLClientValidator


def _make_charm(databag: dict[str, str], endpoint: str = "db") -> MagicMock:
    """Build a minimal charm stub with a single relation app databag."""
    relation = MagicMock()
    relation.app = MagicMock()
    relation.data = {relation.app: databag}

    charm = MagicMock()
    charm.model.get_relation.return_value = relation
    return charm


VALID_DATABAG: dict[str, str] = {
    "endpoints": "10.1.2.3:5432",
    "database": "mydb",
    "username": "myuser",
    "password": "mypassword",
}


class TestPostgreSQLClientValidatorSimple:
    def test_returns_error_for_unsupported_level(self) -> None:
        # GIVEN
        charm = _make_charm(VALID_DATABAG)
        validator = PostgreSQLClientValidator(charm, "db")

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "ERROR"
        assert result.error is not None
        assert "not yet implemented" in result.error

    def test_returns_error_when_relation_is_none(self) -> None:
        # GIVEN a charm with no relation
        charm = MagicMock()
        charm.model.get_relation.return_value = None
        validator = PostgreSQLClientValidator(charm, "db")

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"
        assert result.error is not None

    def test_returns_error_when_relation_app_is_none(self) -> None:
        # GIVEN a relation with no app
        relation = MagicMock()
        relation.app = None
        charm = MagicMock()
        charm.model.get_relation.return_value = relation
        validator = PostgreSQLClientValidator(charm, "db")

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"

    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        # GIVEN a databag with missing required fields
        charm = _make_charm({"endpoints": "10.1.2.3:5432"})
        validator = PostgreSQLClientValidator(charm, "db")

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
        charm = _make_charm(VALID_DATABAG)
        validator = PostgreSQLClientValidator(charm, "db")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=mock_conn):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed

    def test_fails_connect_check_when_db_unreachable(self) -> None:
        # GIVEN a complete databag but a DB that refuses connections
        charm = _make_charm(VALID_DATABAG)
        validator = PostgreSQLClientValidator(charm, "db")

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
        # GIVEN a connection that succeeds but raises on SELECT 1
        charm = _make_charm(VALID_DATABAG)
        validator = PostgreSQLClientValidator(charm, "db")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = psycopg2.DatabaseError("query error")
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=mock_conn):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        query_check = next(c for c in result.checks if c.name == "query")
        assert not query_check.passed

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        charm = _make_charm(VALID_DATABAG)
        validator = PostgreSQLClientValidator(charm, "my-endpoint")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=mock_conn):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-endpoint"
        assert result.interface == "postgresql_client"
