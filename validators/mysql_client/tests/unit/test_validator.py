# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import pymysql
import pytest

from validators.mysql_client.validator import MySQLClientValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _make_validator(
    databag: dict[str, str], endpoint: str = "database", role: RelationRoleStub = RelationRoleStub.requires
) -> MySQLClientValidator:
    app = ApplicationStub()
    relation = RelationStub(app=app, data={app: databag}, name=endpoint, id=0)
    charm = make_charm_from_relation(relation, interface_name="mysql_client", role=role)
    return MySQLClientValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


@dataclass
class CursorStub:
    """Minimal cursor context manager; raises execute_error if set."""

    execute_error: Exception | None = None
    # Rows returned by fetchone() for each successive call.
    fetchone_rows: list[tuple[Any, ...]] = field(default_factory=list)
    # Number of execute() calls to allow before raising execute_error.
    execute_succeed_count: int = 0
    lastrowid: int = 1
    _fetch_count: int = field(default=0, init=False, repr=False)
    _execute_count: int = field(default=0, init=False, repr=False)

    def execute(self, query: str, params: Any = None) -> None:
        if self.execute_error and self._execute_count >= self.execute_succeed_count:
            raise self.execute_error
        self._execute_count += 1

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._fetch_count < len(self.fetchone_rows):
            row = self.fetchone_rows[self._fetch_count]
            self._fetch_count += 1
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

    def cursor(self) -> CursorStub:
        return self.cursor_stub

    def autocommit(self, value: bool) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "endpoints": "10.1.2.3:3306",
    "database": "mydb",
    "username": "myuser",
    "password": "mypassword",
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMySQLClientValidatorSimple:
    @pytest.mark.parametrize(
        "role,should_skip",
        [(RelationRoleStub.requires, False), (RelationRoleStub.provides, True), (RelationRoleStub.peer, True)],
    )
    def test_returns_skipped_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, role=role)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

    def test_returns_skipped_for_unsupported_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None
        assert "not supported" in result.error

    def test_returns_error_when_no_remote_app(self) -> None:
        # GIVEN a relation with no data for the remote app (relation.app is None)
        relation = RelationStub(name="database", id=0, app=None, data={})
        charm = make_charm_from_relation(relation, interface_name="mysql_client")
        validator = MySQLClientValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"
        assert result.error is not None
        assert "No remote application" in result.error

    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        # GIVEN a databag with missing required fields
        validator = _make_validator({"endpoints": "10.1.2.3:3306"})

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
        conn = ConnStub(cursor_stub=CursorStub(fetchone_rows=[(1,)]))
        with patch("validators.mysql_client.validator.pymysql.connect", return_value=conn):
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
            "validators.mysql_client.validator.pymysql.connect",
            side_effect=pymysql.err.OperationalError("Connection refused"),
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
        conn = ConnStub(cursor_stub=CursorStub(execute_error=pymysql.err.OperationalError("query error")))

        with patch("validators.mysql_client.validator.pymysql.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        query_check = next(c for c in result.checks if c.name == "query")
        assert not query_check.passed

    def test_version_consistency_check_flags_mismatch(self) -> None:
        # GIVEN a databag declaring a version that doesn't match the server-reported version
        databag = {**VALID_DATABAG, "version": "8.0.99"}
        validator = _make_validator(databag)
        conn = ConnStub(cursor_stub=CursorStub(fetchone_rows=[("8.0.45",)]))

        with patch("validators.mysql_client.validator.pymysql.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        version_check = next(c for c in result.checks if c.name == "version_consistency")
        assert not version_check.passed

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-endpoint")
        conn = ConnStub(cursor_stub=CursorStub(fetchone_rows=[(1,)]))

        with patch("validators.mysql_client.validator.pymysql.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-endpoint"
        assert result.interface == "mysql_client"


class TestMySQLClientValidatorDeep:
    def test_returns_skipped_for_uat_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_deep_passes_on_successful_write_read_verify(self) -> None:
        # GIVEN a complete databag and a successful connection with write/read
        validator = _make_validator(VALID_DATABAG)
        conn = ConnStub(cursor_stub=CursorStub(fetchone_rows=[("validator-probe",)]))
        with patch("validators.mysql_client.validator.pymysql.connect", return_value=conn):
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

    def test_deep_fails_when_write_fails(self) -> None:
        # GIVEN a connection that fails on INSERT
        validator = _make_validator(VALID_DATABAG)
        conn = ConnStub(
            cursor_stub=CursorStub(
                fetchone_rows=[(1,)],
                execute_error=pymysql.err.OperationalError("Write failed"),
                execute_succeed_count=2,
            )
        )
        with patch("validators.mysql_client.validator.pymysql.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        write_check = next(c for c in result.checks if c.name == "write_read_verify")
        assert not write_check.passed

    def test_deep_fails_when_read_verification_fails(self) -> None:
        # GIVEN a connection where the read-back row doesn't match the written marker
        validator = _make_validator(VALID_DATABAG)
        conn = ConnStub(cursor_stub=CursorStub(fetchone_rows=[("unexpected-value",)]))
        with patch("validators.mysql_client.validator.pymysql.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        write_check = next(c for c in result.checks if c.name == "write_read_verify")
        assert not write_check.passed
        assert "Failed to verify" in write_check.message
