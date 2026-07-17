# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import pymysql
import pytest

from validators.mysql.validator import MySQLValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
    UnitStub,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class CursorStub:
    """Minimal cursor context manager; raises execute_error if set."""

    execute_error: Exception | None = None
    fetchone_rows: list[tuple[Any, ...]] = field(default_factory=list)
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
# Factory
# ---------------------------------------------------------------------------


def _make_validator(
    conn_data: dict[str, str],
    role: RelationRoleStub = RelationRoleStub.requires,
    endpoint: str = "mysql",
) -> MySQLValidator:
    """Build a validator whose connection data lives on the correct unit databag.

    The legacy ``mysql`` interface is unit scoped: for the provider role the
    details are on the local unit databag, for the requirer role they are on the
    remote provider unit databag.
    """
    app = ApplicationStub()
    unit = UnitStub("mysql/0")

    if role == RelationRoleStub.provides:
        relation = RelationStub(app=app, data={app: {}, unit: dict(conn_data)}, name=endpoint, id=0)
        charm = make_charm_from_relation(relation, interface_name="mysql", role=role)
        charm.unit = unit
    else:
        relation = RelationStub(
            app=app,
            data={app: {}, unit: dict(conn_data)},
            name=endpoint,
            id=0,
            units=frozenset({unit}),
        )
        charm = make_charm_from_relation(relation, interface_name="mysql", role=role)

    return MySQLValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


VALID_DATABAG: dict[str, str] = {
    "host": "10.1.2.3",
    "port": "3306",
    "user": "myuser",
    "password": "mypassword",
    "database": "mydb",
}

_BOTH_ROLES = [RelationRoleStub.requires, RelationRoleStub.provides]


# ---------------------------------------------------------------------------
# Role and level gating
# ---------------------------------------------------------------------------


class TestGating:
    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.requires, False),
            (RelationRoleStub.provides, False),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_role_gating(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN a validator for a given relation role
        validator = _make_validator(VALID_DATABAG, role=role)
        conn = ConnStub(cursor_stub=CursorStub(fetchone_rows=[(1,)]))

        # WHEN validating
        with patch("validators.mysql.validator.pymysql.connect", return_value=conn):
            result = validator.validate(level="simple")

        # THEN peer is skipped, requires/provides are not
        assert (result.status == "SKIPPED") == should_skip

    @pytest.mark.parametrize("role", _BOTH_ROLES)
    def test_unsupported_level_is_skipped(self, role: RelationRoleStub) -> None:
        # GIVEN a validator
        validator = _make_validator(VALID_DATABAG, role=role)

        # WHEN validating at an unsupported level
        result = validator.validate(level="uat")

        # THEN it is skipped with an explanatory error
        assert result.status == "SKIPPED"
        assert result.error is not None
        assert "not supported" in result.error

    @pytest.mark.parametrize("role", _BOTH_ROLES)
    def test_no_remote_app_is_error(self, role: RelationRoleStub) -> None:
        # GIVEN a relation with no remote application
        relation = RelationStub(name="mysql", id=0, app=None, data={})
        charm = make_charm_from_relation(relation, interface_name="mysql", role=role)
        validator = MySQLValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN validating
        result = validator.validate(level="simple")

        # THEN an ERROR result is returned
        assert result.status == "ERROR"
        assert result.error is not None
        assert "No remote application" in result.error


# ---------------------------------------------------------------------------
# L1 (simple) — one valid + two invalid per role
# ---------------------------------------------------------------------------


class TestSimple:
    @pytest.mark.parametrize("role", _BOTH_ROLES)
    def test_valid_passes(self, role: RelationRoleStub) -> None:
        # GIVEN a complete databag and a reachable server
        validator = _make_validator(VALID_DATABAG, role=role)
        conn = ConnStub(cursor_stub=CursorStub(fetchone_rows=[(1,)]))

        # WHEN validating at L1
        with patch("validators.mysql.validator.pymysql.connect", return_value=conn):
            result = validator.validate(level="simple")

        # THEN all checks pass
        assert result.status == "PASS"
        assert {c.name for c in result.checks} >= {"schema", "field_constraints", "connect", "query"}

    @pytest.mark.parametrize("role", _BOTH_ROLES)
    def test_invalid_missing_fields_fails_schema(self, role: RelationRoleStub) -> None:
        # GIVEN a databag missing required credential fields
        validator = _make_validator({"host": "10.1.2.3"}, role=role)

        # WHEN validating at L1
        result = validator.validate(level="simple")

        # THEN the schema check fails and names the missing fields
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        for missing in ("user", "password", "database"):
            assert missing in schema.message

    @pytest.mark.parametrize("role", _BOTH_ROLES)
    def test_invalid_bad_port_fails_constraints(self, role: RelationRoleStub) -> None:
        # GIVEN a databag with an out-of-range port
        databag = {**VALID_DATABAG, "port": "70000"}
        validator = _make_validator(databag, role=role)

        # WHEN validating at L1
        result = validator.validate(level="simple")

        # THEN the field-constraints check fails
        assert result.status == "FAIL"
        fields = next(c for c in result.checks if c.name == "field_constraints")
        assert not fields.passed
        assert "port" in fields.message

    @pytest.mark.parametrize("role", _BOTH_ROLES)
    def test_invalid_unreachable_server_fails_connect(self, role: RelationRoleStub) -> None:
        # GIVEN a complete databag but a server that refuses connections
        validator = _make_validator(VALID_DATABAG, role=role)

        # WHEN validating at L1
        with patch(
            "validators.mysql.validator.pymysql.connect",
            side_effect=pymysql.err.OperationalError("Connection refused"),
        ):
            result = validator.validate(level="simple")

        # THEN the connect check fails with an actionable message
        assert result.status == "FAIL"
        connect = next(c for c in result.checks if c.name == "connect")
        assert not connect.passed
        assert "Connection refused" in connect.message


# ---------------------------------------------------------------------------
# L2 (deep) — one valid + two invalid per role
# ---------------------------------------------------------------------------


class TestDeep:
    @pytest.mark.parametrize("role", _BOTH_ROLES)
    def test_valid_passes(self, role: RelationRoleStub) -> None:
        # GIVEN a complete databag and a server that round-trips the canary row
        validator = _make_validator(VALID_DATABAG, role=role)
        conn = ConnStub(cursor_stub=CursorStub(fetchone_rows=[("validator-probe",)]))

        # WHEN validating at L2
        with patch("validators.mysql.validator.pymysql.connect", return_value=conn):
            result = validator.validate(level="deep")

        # THEN the write/read/verify, cleanup and latency checks pass
        assert result.status == "PASS"
        assert result.level == "deep"
        for name in ("write_read_verify", "cleanup", "latency"):
            assert next(c for c in result.checks if c.name == name).passed

    @pytest.mark.parametrize("role", _BOTH_ROLES)
    def test_invalid_write_failure_fails(self, role: RelationRoleStub) -> None:
        # GIVEN a server that fails on the INSERT (second execute)
        validator = _make_validator(VALID_DATABAG, role=role)
        conn = ConnStub(
            cursor_stub=CursorStub(
                fetchone_rows=[("validator-probe",)],
                execute_error=pymysql.err.OperationalError("Write failed"),
                execute_succeed_count=1,
            )
        )

        # WHEN validating at L2
        with patch("validators.mysql.validator.pymysql.connect", return_value=conn):
            result = validator.validate(level="deep")

        # THEN the write/read/verify check fails
        assert result.status == "FAIL"
        write = next(c for c in result.checks if c.name == "write_read_verify")
        assert not write.passed

    @pytest.mark.parametrize("role", _BOTH_ROLES)
    def test_invalid_read_mismatch_fails(self, role: RelationRoleStub) -> None:
        # GIVEN a server that returns a different value than was written
        validator = _make_validator(VALID_DATABAG, role=role)
        conn = ConnStub(cursor_stub=CursorStub(fetchone_rows=[("unexpected-value",)]))

        # WHEN validating at L2
        with patch("validators.mysql.validator.pymysql.connect", return_value=conn):
            result = validator.validate(level="deep")

        # THEN the write/read/verify check fails on the mismatch
        assert result.status == "FAIL"
        write = next(c for c in result.checks if c.name == "write_read_verify")
        assert not write.passed
        assert "did not match" in write.message
