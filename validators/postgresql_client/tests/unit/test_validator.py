# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import psycopg2
import pytest
from pydantic import ValidationError

from validators.base import PersistenceNotApplicable, PersistenceState
from validators.postgresql_client.validator import (
    PostgreSQLClientPersistenceValidator,
    PostgreSQLClientValidator,
)
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

# Arbitrary non-empty token used by checkpoint() tests; prepare() generates a random one per run.
TEST_TOKEN = "test-token-abc123"


def _make_validator(
    databag: dict[str, str], endpoint: str = "db", role: RelationRoleStub = RelationRoleStub.requires
) -> PostgreSQLClientValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    charm = cast(ops.CharmBase, make_charm_from_relation(relation, interface_name="postgresql_client", role=role))
    return PostgreSQLClientValidator(charm, cast(ops.Relation, relation))


def _make_persistence_validator(
    databag: dict[str, str],
    endpoint: str = "db",
    role: RelationRoleStub = RelationRoleStub.requires,
    relation_id: int = 0,
    model_uuid: str = "11111111-1111-1111-1111-111111111111",
    unit_name: str = "app/0",
) -> PostgreSQLClientPersistenceValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=relation_id, app=app, data={app: databag})
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(
            relation,
            interface_name="postgresql_client",
            role=role,
            local_model_uuid=model_uuid,
            local_unit_name=unit_name,
        ),
    )
    return PostgreSQLClientPersistenceValidator(charm, cast(ops.Relation, relation))


@dataclass
class CursorStub:
    """Minimal cursor context manager; raises execute_error if set."""

    execute_error: Exception | None = None
    # Rows returned by fetchone() for each successive call. An entry of None simulates a query
    # that found no matching row (e.g. an information_schema lookup for a nonexistent table).
    fetchone_rows: list[tuple[Any, ...] | None] = field(default_factory=list)
    # Rows returned by fetchall().
    fetchall_rows: list[tuple[Any, ...]] = field(default_factory=list)
    # Number of execute() calls to allow before raising execute_error.
    execute_succeed_count: int = 0
    _fetch_count: int = field(default=0, init=False, repr=False)
    _execute_count: int = field(default=0, init=False, repr=False)
    executed_queries: list[str] = field(default_factory=list, init=False, repr=False)
    executed_params: list[Any] = field(default_factory=list, init=False, repr=False)

    def execute(self, query: str, params: Any = None) -> None:
        self.executed_queries.append(query)
        self.executed_params.append(params)
        if self.execute_error and self._execute_count >= self.execute_succeed_count:
            raise self.execute_error
        self._execute_count += 1

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._fetch_count < len(self.fetchone_rows):
            row = self.fetchone_rows[self._fetch_count]
            self._fetch_count += 1
            return row
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.fetchall_rows

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

    @pytest.mark.parametrize(
        "role,should_skip",
        [(RelationRoleStub.requires, False), (RelationRoleStub.provides, True), (RelationRoleStub.peer, True)],
    )
    def test_skips_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN a validator with a non-requires role
        validator = _make_validator(VALID_DATABAG, role=role)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

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
        query_check = next(c for c in result.checks if c.name == "query")
        assert query_check.passed
        write_check = next(c for c in result.checks if c.name == "write_read_verify")
        assert write_check.passed
        cleanup_check = next(c for c in result.checks if c.name == "cleanup")
        assert cleanup_check.passed
        latency_check = next(c for c in result.checks if c.name == "latency")
        assert latency_check.passed

    def test_deep_latency_excludes_credential_resolution_time(self) -> None:
        # Regression test for: cross-model/secrets-based credential resolution
        # (Juju secret-get) can be slow independent of the database itself, and
        # must not be counted against the deep-validation latency budget.
        # GIVEN credential resolution alone consumes more than the 10s timeout
        # (simulated via a fake clock so the test runs instantly), but the
        # database round trip itself is instantaneous.
        validator = _make_validator(VALID_DATABAG)
        cursor = CursorStub(fetchone_rows=[(42,), ("validator-probe",)])
        conn = ConnStub(cursor_stub=cursor)

        class FakeClock:
            def __init__(self) -> None:
                self._now = 0.0

            def monotonic(self) -> float:
                return self._now

            def sleep(self, seconds: float) -> None:
                self._now += seconds

        fake_clock = FakeClock()
        real_resolve_credentials = validator._resolve_credentials

        def _slow_resolve_credentials() -> dict[str, str]:
            fake_clock.sleep(11)  # simulate a slow Juju secret-get round trip
            return real_resolve_credentials()

        with (
            patch.object(validator, "_resolve_credentials", side_effect=_slow_resolve_credentials),
            patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn),
            patch("validators.postgresql_client.validator.time", fake_clock),
        ):
            # WHEN
            result = validator.validate(level="deep")

        # THEN the latency check still passes because timing starts after
        # credentials are resolved, not from the top of the function.
        latency_check = next(c for c in result.checks if c.name == "latency")
        assert latency_check.passed, latency_check.message

    def test_deep_sets_autocommit_before_any_cursor(self) -> None:
        # Regression test for: "set_session cannot be used inside a transaction"
        # GIVEN a connection that raises ProgrammingError if autocommit is set
        # after a cursor has already been opened (i.e., inside a transaction).
        validator = _make_validator(VALID_DATABAG)
        cursor_opened: list[bool] = []

        class AutocommitGuardConn(ConnStub):
            """Raises if autocommit is set after any cursor has been opened."""

            _autocommit: bool = False

            @property
            def autocommit(self) -> bool:
                return self._autocommit

            @autocommit.setter
            def autocommit(self, value: bool) -> None:
                if cursor_opened:
                    raise psycopg2.ProgrammingError("set_session cannot be used inside a transaction")
                self._autocommit = value

            def cursor(self) -> CursorStub:
                cursor_opened.append(True)
                return self.cursor_stub

        conn = AutocommitGuardConn(cursor_stub=CursorStub(fetchone_rows=[(42,), ("validator-probe",)]))

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            result = validator.validate(level="deep")

        # THEN autocommit was set before any cursor was opened — no ProgrammingError
        assert result.status == "PASS"
        write_check = next(c for c in result.checks if c.name == "write_read_verify")
        assert write_check.passed

    def test_deep_fails_when_query_raises(self) -> None:
        # GIVEN a connection that raises on SELECT 1 (before write block)
        validator = _make_validator(VALID_DATABAG)
        cursor = CursorStub(execute_error=psycopg2.DatabaseError("query error"))
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        query_check = next(c for c in result.checks if c.name == "query")
        assert not query_check.passed
        assert "query error" in query_check.message
        # Should stop before attempting write
        assert not any(c.name == "write_read_verify" for c in result.checks)

    def test_deep_fails_when_canary_write_raises(self) -> None:
        # GIVEN SELECT 1 succeeds but an error occurs in the canary write block
        validator = _make_validator(VALID_DATABAG)
        cursor = CursorStub(execute_error=psycopg2.DatabaseError("write error"), execute_succeed_count=1)
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


class TestPostgreSQLClientPersistenceValidatorRole:
    @pytest.mark.parametrize(
        "role",
        [RelationRoleStub.provides, RelationRoleStub.peer],
    )
    def test_prepare_raises_not_applicable_for_non_requires_role(self, role: RelationRoleStub) -> None:
        # GIVEN a validator on the non-requires side of the relation
        validator = _make_persistence_validator(VALID_DATABAG, role=role)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.prepare()

    def test_checkpoint_raises_not_applicable_for_non_requires_role(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG, role=RelationRoleStub.provides)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

    def test_cleanup_raises_not_applicable_for_non_requires_role(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG, role=RelationRoleStub.provides)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.cleanup()


class TestPostgreSQLClientPersistenceValidatorConnection:
    def test_prepare_raises_when_uris_is_blank(self) -> None:
        # GIVEN a databag with a present but blank "uris" field
        # Regression test for: a blank uri was previously passed straight to psycopg2 as
        # dsn="", which libpq treats as "use local/default connection parameters" instead of
        # failing - silently connecting to an unintended database rather than erroring on
        # missing relation credentials.
        databag = {**VALID_DATABAG, "uris": ""}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="uris"):
            validator.prepare()

    def test_checkpoint_raises_when_uris_is_missing(self) -> None:
        # GIVEN a databag missing the "uris" field entirely
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "uris"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="uris"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

    def test_prepare_raises_when_first_uri_is_blank_after_split(self) -> None:
        # GIVEN a "uris" value that is non-blank (so validate_schema() passes) but whose first
        # comma-separated entry is blank once split/stripped - e.g. a leading comma or a
        # whitespace-only first entry.
        # Regression test for: this previously reached _connect() as dsn="", which libpq treats
        # as "use local/default connection parameters" instead of failing loudly.
        databag = {**VALID_DATABAG, "uris": " ,postgresql://10.1.2.3:5432/mydb"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="uris"):
            validator.prepare()

    def test_prepare_raises_when_uri_database_does_not_match_databag_database(self) -> None:
        # GIVEN a "uris" value pointing at "mydb" but a "database" field claiming a different
        # database.
        # Regression test for: _open_connection() (used by prepare()/checkpoint()/cleanup())
        # previously skipped the consistency check _validate_simple()/_validate_deep() perform, so
        # canary data would be written and verified against the wrong database.
        databag = {**VALID_DATABAG, "database": "otherdb"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="does not match"):
            validator.prepare()


class TestPostgreSQLClientPersistenceValidatorPrepare:
    def test_creates_canary_table_and_returns_state(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)
        conn = ConnStub()

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            state = validator.prepare()

        # THEN
        assert isinstance(state, PersistenceState)
        assert state.ref == 1
        assert state.token
        queries = " ".join(conn.cursor_stub.executed_queries)
        assert f"validator_canary_da7d88bc9ad4d4fd_{state.id:020d}" in queries
        assert "DROP TABLE IF EXISTS" in queries
        assert "CREATE TABLE" in queries
        assert "INSERT INTO" in queries
        # The token is written as the row's marker, so checkpoint() can match on it later
        insert_params = conn.cursor_stub.executed_params[-1]
        assert insert_params[0] == state.token

    def test_generates_distinct_identifiers_across_calls(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=ConnStub()):
            # WHEN
            first = validator.prepare()
            second = validator.prepare()

        # THEN
        assert first.id != second.id
        assert first.token != second.token


class TestPostgreSQLClientPersistenceValidatorCheckpoint:
    def test_passes_when_row_count_matches_expected_ref(self) -> None:
        # GIVEN the canary table has exactly the expected number of rows with matching identity
        validator = _make_persistence_validator(VALID_DATABAG)
        # Schema-resolution query (fetchall) finds one match; then the id=checkpoint_ref
        # identity-matching count query (fetchone) returns 2.
        cursor = CursorStub(fetchall_rows=[("public",)], fetchone_rows=[(2,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN
        assert result.status == "PASS"
        check = next(c for c in result.checks if c.name == "row_count")
        assert check.passed
        assert new_state.id == 42
        assert new_state.ref == 3
        assert new_state.token == TEST_TOKEN
        # A new row is still written to continue the chain
        assert any("INSERT INTO" in q for q in cursor.executed_queries)

    def test_fails_when_row_count_is_lower_than_expected(self) -> None:
        # GIVEN data loss: fewer matching rows than expected
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[("public",)], fetchone_rows=[(1,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=7, ref=3))

        # THEN
        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "row_count")
        assert not check.passed
        assert "3" in check.message and "1" in check.message
        # Regression test for: checkpoint() previously wrote a new marker row and advanced `ref`
        # even on FAIL, but ValidatorRunner only carries the returned state forward on PASS, so the
        # actual row count would drift past what a later checkpoint could compare against. On FAIL
        # the returned state must match `expected` unchanged and no INSERT should be issued.
        assert new_state == PersistenceState(token=TEST_TOKEN, id=7, ref=3)
        assert not any("INSERT INTO" in q for q in cursor.executed_queries)

    def test_fails_when_table_is_not_found(self) -> None:
        # GIVEN the canary table doesn't exist in any schema (e.g. it was dropped/never created)
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=7, ref=1))

        # THEN
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=7, ref=1)
        assert not any("INSERT INTO" in q for q in cursor.executed_queries)

    def test_uses_canary_table_name_from_expected_identifier(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[("public",)], fetchone_rows=[(1,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN
        assert any("validator_canary_da7d88bc9ad4d4fd_00000000000000000099" in q for q in cursor.executed_queries)

    def test_resolves_schema_by_exact_name_across_all_schemas_regardless_of_visibility(self) -> None:
        # GIVEN a single table anywhere in the database matches this canary's exact (random,
        # effectively-unique) name.
        # Regression test for: resolving the schema via pg_table_is_visible() alone depends on the
        # *current* connection's search_path, which can disagree with the one prepare() used,
        # causing a false FAIL.
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[("some_schema", False)], fetchone_rows=[(1,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result, _ = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN the single match is used even though it isn't currently visible
        assert result.status == "PASS"
        schema_query = next(q for q in cursor.executed_queries if "pg_catalog.pg_class" in q)
        assert "pg_catalog.pg_namespace" in schema_query

    def test_tie_breaks_multiple_same_named_tables_by_search_path_visibility(self) -> None:
        # GIVEN the (vanishingly unlikely, but not impossible) case where more than one schema
        # contains a same-named table - e.g. a leftover canary from an earlier, interrupted run
        # coincidentally reusing this run's random name. Genuine ambiguity like this must be
        # resolved the same way PostgreSQL itself would resolve an unqualified reference: whichever
        # match is visible under the *current* search_path.
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[("stale_schema", False), ("public", True)], fetchone_rows=[(1,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN the visible schema ("public") is used to qualify the count query, not the first row
        count_query_index = next(i for i, q in enumerate(cursor.executed_queries) if "COUNT(*)" in q)
        assert '"public".' in cursor.executed_queries[count_query_index]

    def test_filters_row_count_by_token(self) -> None:
        # GIVEN
        # Regression test for: checkpoint() previously counted every row in the table, so a table
        # recreated from scratch with an unrelated but equally-sized set of rows would still pass.
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[("public",)], fetchone_rows=[(1,)])  # schema, then identity-matching count
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN the row count query and the follow-up insert are both scoped to the same token,
        # which is random per prepare() run so it cannot be reproduced by a recreated table
        select_index = next(i for i, q in enumerate(cursor.executed_queries) if "COUNT(*)" in q)
        insert_index = next(i for i, q in enumerate(cursor.executed_queries) if "INSERT INTO" in q)
        assert "WHERE marker = %s" in cursor.executed_queries[select_index]
        assert cursor.executed_params[select_index] == (TEST_TOKEN, 1)
        # INSERT params now include checkpoint_ref and written_at (the token is first)
        assert cursor.executed_params[insert_index][0] == TEST_TOKEN

    def test_fails_when_table_was_dropped_and_recreated_with_same_ref(self) -> None:
        # GIVEN a table dropped and recreated from scratch: the SERIAL sequence resets, so
        # reinserting rows with the same checkpoint_ref reproduces `id == checkpoint_ref` and the
        # same row count. Only the random per-run token distinguishes the original canary rows from
        # the recreated ones, so the count query (scoped to the original token) matches nothing.
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[("public",)], fetchone_rows=[(0,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN the recreated table is detected as data loss, not a false PASS
        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "row_count")
        assert not check.passed
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=2)
        assert not any("INSERT INTO" in q for q in cursor.executed_queries)

    def test_rejects_state_without_a_token(self) -> None:
        # GIVEN a state serialised before the token existed (or otherwise restored/malformed)
        # Regression test for: matching on an empty marker would count rows carrying no token at
        # all, silently degrading the identity check instead of failing safely. The base protocol
        # rejects such a state at construction, so it can never reach checkpoint().
        with pytest.raises(ValidationError):
            PersistenceState(id=1, ref=1)

    def test_result_endpoint_and_interface_are_set(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG, endpoint="my-db")
        cursor = CursorStub(fetchall_rows=[("public",)], fetchone_rows=[(1,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            result, _ = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

        # THEN
        assert result.endpoint == "my-db"
        assert result.interface == "postgresql_client"
        assert result.level == "deep"

    def test_raises_when_expected_identifier_is_out_of_range(self) -> None:
        # GIVEN
        # Regression test for: checkpoint() previously formatted expected.id into the table name
        # without validating it, so a restored/malformed PersistenceState with an out-of-range id
        # (larger than any identifier prepare() can produce, masked to 63 bits) could silently
        # produce an overlong/invalid table name instead of failing safely.
        validator = _make_persistence_validator(VALID_DATABAG)
        out_of_range_id = 1 << 63  # one past _MAX_CANARY_IDENTIFIER

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=ConnStub()):
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=out_of_range_id, ref=1))

    def test_raises_when_expected_identifier_is_negative(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=ConnStub()):
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=-1, ref=1))

    def test_raises_when_expected_ref_is_zero(self) -> None:
        # GIVEN
        # Regression test for: checkpoint() previously compared `actual == expected.ref` without
        # validating expected.ref first, so a restored/malformed PersistenceState with ref=0 could
        # let an empty or partially recreated table (actual == 0) coincidentally satisfy the
        # comparison and report a false PASS. prepare() always returns ref=1, so ref < 1 can never
        # have come from a real prior run.
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=ConnStub()):
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=0))

    def test_raises_when_expected_ref_is_negative(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=ConnStub()):
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=-1))


class TestPostgreSQLClientPersistenceValidatorCleanup:
    def test_drops_all_discovered_canary_tables(self) -> None:
        # GIVEN two leftover canary tables are discovered, in the current schema
        validator = _make_persistence_validator(VALID_DATABAG)
        table_1 = "validator_canary_da7d88bc9ad4d4fd_00000000000000000001"
        table_2 = "validator_canary_da7d88bc9ad4d4fd_00000000000000000002"
        cursor = CursorStub(fetchall_rows=[("public", table_1), ("public", table_2)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        drop_queries = [q for q in cursor.executed_queries if "DROP TABLE" in q]
        assert any(table_1 in q for q in drop_queries)
        assert any(table_2 in q for q in drop_queries)
        # THEN dropped identifiers are safely quoted (defense in depth: they come from
        # information_schema, not directly from user input, but should not be trusted blindly)
        # and schema-qualified, so a same-named table in another schema can't be targeted instead.
        assert all('"public"."validator_canary_' in q for q in drop_queries)

    def test_searches_all_schemas_for_discovery(self) -> None:
        # Regression test for: an unqualified information_schema query and DROP TABLE can miss a
        # canary outside the connection's current schema, or drop an unrelated same-named object
        # in a different schema. Discovery must search all schemas, not just current_schema().
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        select_query = next(q for q in cursor.executed_queries if "information_schema" in q)
        # Query should NOT restrict to current_schema() only, since an unqualified CREATE TABLE
        # resolves through search_path to the first writable schema, which may not be current_schema().
        # We search all schemas to ensure we find and drop canary tables regardless of which schema
        # PostgreSQL chose for the unqualified CREATE TABLE.
        assert "current_schema()" not in select_query
        assert "WHERE" in select_query  # Should still have some filtering (by table_type and LIKE pattern)

    def test_scopes_discovery_to_this_relations_id(self) -> None:
        # Regression test for: discovery previously matched the bare `validator_canary_` prefix
        # shared by every relation, so cleanup for one `postgresql_client` relation could drop
        # canary tables belonging to a different, concurrent relation on the same database/schema.
        # The LIKE pattern must be scoped to a token derived from this validator's own
        # model+relation_id.
        validator = _make_persistence_validator(VALID_DATABAG, relation_id=7)
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        select_query = next(q for q in cursor.executed_queries if "information_schema" in q)
        assert "table_name LIKE %s" in select_query
        assert cursor.executed_params[0] == ("validator\\_canary\\_9c1f7f01a01956b9\\_%",)

    def test_scopes_discovery_to_this_models_uuid(self) -> None:
        # Regression test for: relation IDs are assigned independently per model, so two
        # different models can expose the same relation_id for a postgresql_client relation to
        # the same shared database/schema. Discovery must also be scoped to a model-specific
        # token so cleanup in one model can't drop another model's canary tables sharing the
        # same relation_id.
        validator = _make_persistence_validator(
            VALID_DATABAG, relation_id=7, model_uuid="22222222-2222-2222-2222-222222222222"
        )
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN the LIKE pattern is scoped to this model's own token, not the other model's
        select_query = next(q for q in cursor.executed_queries if "information_schema" in q)
        assert "table_name LIKE %s" in select_query
        like_pattern = cursor.executed_params[0][0]
        assert like_pattern != "validator\\_canary\\_9c1f7f01a01956b9\\_%"

    def test_scopes_discovery_to_this_unit(self) -> None:
        # Regression test for: the harness runs persistence validators on every unit of the
        # application, so two units of the same application share both model.uuid and relation_id
        # while owning separate canary tables. Discovery scoped to model+relation only would let
        # cleanup on one unit drop another unit's still-active table, making that unit's next
        # checkpoint report a data loss that never happened. The token must include the unit name.
        validator = _make_persistence_validator(VALID_DATABAG, relation_id=7, unit_name="app/0")
        other_unit = _make_persistence_validator(VALID_DATABAG, relation_id=7, unit_name="app/1")
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN the LIKE pattern is scoped to this unit's own token, not the sibling unit's
        select_query = next(q for q in cursor.executed_queries if "information_schema" in q)
        assert "table_name LIKE %s" in select_query
        assert cursor.executed_params[0] == ("validator\\_canary\\_9c1f7f01a01956b9\\_%",)
        assert other_unit._canary_table_prefix() != validator._canary_table_prefix()

    def test_rejects_discovered_tables_that_only_share_the_prefix(self) -> None:
        # Regression test for: the information_schema LIKE query only narrows candidates by
        # *prefix*, so a same-prefixed but unrelated table (e.g. a hand-created backup table)
        # would previously be dropped unconditionally. Cleanup must re-check the exact shape
        # (prefix + fixed-width digits) before dropping, and skip anything that doesn't match.
        validator = _make_persistence_validator(VALID_DATABAG)
        good_table = "validator_canary_da7d88bc9ad4d4fd_00000000000000000001"
        look_alike = "validator_canary_da7d88bc9ad4d4fd_backup"
        cursor = CursorStub(fetchall_rows=[("public", good_table), ("public", look_alike)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        drop_queries = [q for q in cursor.executed_queries if "DROP TABLE" in q]
        assert any(good_table in q for q in drop_queries)
        assert not any(look_alike in q for q in drop_queries)

    def test_rejects_discovered_tables_with_an_out_of_range_identifier(self) -> None:
        # Regression test for: the discovery regex only checked the *shape* of the identifier
        # suffix (20 digits), but prepare() masks identifiers to 63 bits (max
        # 9223372036854775807, 19 digits) - a 20-digit suffix can represent a value far larger
        # than that. Without an explicit bound check, cleanup would still drop a shape-only
        # look-alike such as "..._99999999999999999999" that prepare() could never have produced.
        validator = _make_persistence_validator(VALID_DATABAG)
        good_table = "validator_canary_da7d88bc9ad4d4fd_00000000000000000001"
        out_of_range_table = "validator_canary_da7d88bc9ad4d4fd_99999999999999999999"
        cursor = CursorStub(fetchall_rows=[("public", good_table), ("public", out_of_range_table)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        drop_queries = [q for q in cursor.executed_queries if "DROP TABLE" in q]
        assert any(good_table in q for q in drop_queries)
        assert not any(out_of_range_table in q for q in drop_queries)

    def test_restricts_discovery_to_base_tables(self) -> None:
        # Regression test for: information_schema.tables also lists views/foreign tables. A view
        # sharing the canary prefix would make DROP TABLE fail and abort cleanup, leaving other
        # discovered canary tables undropped. Discovery must be scoped to table_type = 'BASE TABLE'.
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        select_query = next(q for q in cursor.executed_queries if "information_schema" in q)
        assert "table_type = 'BASE TABLE'" in select_query

    def test_escapes_like_wildcards_in_prefix_pattern(self) -> None:
        # GIVEN
        # Regression test for: the canary table prefix contains underscores, which are LIKE
        # wildcards; an unescaped pattern could match unrelated tables (e.g. "validatorXcanaryY1").
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.postgresql_client.validator.psycopg2.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        select_query = next(q for q in cursor.executed_queries if "information_schema" in q)
        assert "ESCAPE" in select_query
        assert cursor.executed_params[0] == ("validator\\_canary\\_da7d88bc9ad4d4fd\\_%",)

    def test_noop_when_no_credentials_present(self) -> None:
        # GIVEN a databag without any credential fields (e.g. relation already gone)
        validator = _make_persistence_validator({})

        with patch("validators.postgresql_client.validator.psycopg2.connect") as mock_connect:
            # WHEN
            validator.cleanup()

        # THEN no connection was attempted
        mock_connect.assert_not_called()

    def test_noop_when_uris_present_but_other_required_fields_are_missing(self) -> None:
        # GIVEN a relation that has advertised "uris" but not yet the rest of the fields
        # _open_connection() requires (database/username/password) - e.g. still mid-setup.
        # Regression test for: the no-op guard previously only checked "uris"/"secret-user" for
        # presence, so this partial databag would fail that check, fall through to
        # _open_connection(), and raise instead of no-op'ing.
        validator = _make_persistence_validator({"uris": "postgresql://x/y"})

        with patch("validators.postgresql_client.validator.psycopg2.connect") as mock_connect:
            # WHEN
            validator.cleanup()

        # THEN no connection was attempted and no exception was raised
        mock_connect.assert_not_called()
