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


# ---------------------------------------------------------------------------
# Role-specific databag selection (_connection_data)
#
# The legacy ``mysql`` interface is unit scoped, so which databag carries the
# connection details depends on the role:
#   provides -> the provider's own *local* unit databag.
#   requires -> the *remote* provider unit databag(s).
# These tests plant distinguishable decoy data on the wrong databag so that a
# regression in ``_connection_data`` (reading the wrong side) cannot pass by
# coincidentally finding valid-looking fields.
# ---------------------------------------------------------------------------


LOCAL_DATABAG: dict[str, str] = {
    "host": "10.0.0.1",
    "port": "3306",
    "user": "local-user",
    "password": "local-password",
    "database": "local-db",
}

REMOTE_DATABAG: dict[str, str] = {
    "host": "10.0.0.2",
    "port": "3307",
    "user": "remote-user",
    "password": "remote-password",
    "database": "remote-db",
}


class TestConnectionDataSelection:
    def test_provides_reads_local_unit_and_ignores_remote_units(self) -> None:
        # GIVEN a provider whose own unit databag holds the real details while a
        # remote unit databag holds decoy details
        app = ApplicationStub()
        local_unit = UnitStub("mysql/0")
        remote_unit = UnitStub("wordpress/0")
        relation = RelationStub(
            app=app,
            data={app: {}, local_unit: dict(LOCAL_DATABAG), remote_unit: dict(REMOTE_DATABAG)},
            name="mysql",
            id=0,
            units=frozenset({remote_unit}),
        )
        charm = make_charm_from_relation(relation, interface_name="mysql", role=RelationRoleStub.provides)
        charm.unit = local_unit
        validator = MySQLValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN resolving the connection databag
        data = validator._connection_data()

        # THEN the provider reads its own unit databag, not the remote one
        assert data == LOCAL_DATABAG

    def test_requires_reads_remote_provider_unit_and_ignores_local_unit(self) -> None:
        # GIVEN a requirer whose own unit databag holds decoy details while the
        # remote provider unit databag holds the real details
        app = ApplicationStub()
        local_unit = UnitStub("apache-guacamole/0")
        provider_unit = UnitStub("mysql/0")
        relation = RelationStub(
            app=app,
            data={app: {}, local_unit: dict(LOCAL_DATABAG), provider_unit: dict(REMOTE_DATABAG)},
            name="mysql",
            id=0,
            units=frozenset({provider_unit}),
        )
        charm = make_charm_from_relation(relation, interface_name="mysql", role=RelationRoleStub.requires)
        charm.unit = local_unit
        validator = MySQLValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN resolving the connection databag
        data = validator._connection_data()

        # THEN the requirer reads the remote provider unit databag, not its own
        assert data == REMOTE_DATABAG

    def test_requires_selects_single_complete_unit_without_merging(self) -> None:
        # GIVEN two provider units where the lowest-sorted unit is incomplete and
        # a higher-sorted unit publishes a complete, coherent databag
        app = ApplicationStub()
        provider_unit_0 = UnitStub("mysql/0")
        provider_unit_1 = UnitStub("mysql/1")
        complete = {
            "host": "10.0.0.2",
            "port": "3306",
            "user": "u1",
            "password": "p1",
            "database": "db1",
        }
        relation = RelationStub(
            app=app,
            data={
                app: {},
                provider_unit_0: {"host": "10.0.0.1", "user": "u0"},
                provider_unit_1: dict(complete),
            },
            name="mysql",
            id=0,
            units=frozenset({provider_unit_0, provider_unit_1}),
        )
        charm = make_charm_from_relation(relation, interface_name="mysql", role=RelationRoleStub.requires)
        validator = MySQLValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN resolving the connection databag
        data = validator._connection_data()

        # THEN the complete unit databag is returned verbatim, with no fields
        # merged in from the incomplete unit
        assert data == complete

    def test_requires_prefers_lowest_sorted_complete_unit(self) -> None:
        # GIVEN two provider units that both publish complete but different databags
        app = ApplicationStub()
        provider_unit_0 = UnitStub("mysql/0")
        provider_unit_1 = UnitStub("mysql/1")
        first = {**VALID_DATABAG, "host": "10.0.0.1"}
        second = {**VALID_DATABAG, "host": "10.0.0.2"}
        relation = RelationStub(
            app=app,
            data={app: {}, provider_unit_0: dict(first), provider_unit_1: dict(second)},
            name="mysql",
            id=0,
            units=frozenset({provider_unit_0, provider_unit_1}),
        )
        charm = make_charm_from_relation(relation, interface_name="mysql", role=RelationRoleStub.requires)
        validator = MySQLValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN resolving the connection databag
        data = validator._connection_data()

        # THEN the lowest-sorted unit (mysql/0) is chosen deterministically
        assert data == first

    def test_requires_falls_back_to_lowest_unit_when_none_complete(self) -> None:
        # GIVEN two provider units that both publish incomplete databags
        app = ApplicationStub()
        provider_unit_0 = UnitStub("mysql/0")
        provider_unit_1 = UnitStub("mysql/1")
        relation = RelationStub(
            app=app,
            data={
                app: {},
                provider_unit_0: {"host": "10.0.0.1"},
                provider_unit_1: {"user": "u1"},
            },
            name="mysql",
            id=0,
            units=frozenset({provider_unit_0, provider_unit_1}),
        )
        charm = make_charm_from_relation(relation, interface_name="mysql", role=RelationRoleStub.requires)
        validator = MySQLValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN resolving the connection databag
        data = validator._connection_data()

        # THEN the lowest-sorted unit's databag is returned unmerged, so the
        # schema check surfaces a deterministic failure rather than a hybrid
        assert data == {"host": "10.0.0.1"}

    def test_schema_ignores_application_databag(self) -> None:
        # GIVEN a provider whose *application* databag is complete but whose own
        # *unit* databag (the authoritative source for this interface) is missing
        # required fields
        app = ApplicationStub()
        local_unit = UnitStub("mysql/0")
        relation = RelationStub(
            app=app,
            data={app: dict(VALID_DATABAG), local_unit: {"host": "10.0.0.1"}},
            name="mysql",
            id=0,
        )
        charm = make_charm_from_relation(relation, interface_name="mysql", role=RelationRoleStub.provides)
        charm.unit = local_unit
        validator = MySQLValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN validating at L1
        result = validator.validate(level="simple")

        # THEN the schema check fails on the unit databag despite the complete
        # application databag
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        for missing in ("user", "password", "database"):
            assert missing in schema.message
