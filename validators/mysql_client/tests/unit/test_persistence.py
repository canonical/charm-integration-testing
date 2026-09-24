# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import cast
from unittest.mock import patch

import ops
import pytest
from pydantic import ValidationError

from validators.base import PersistenceNotApplicable, PersistenceState
from validators.mysql_client.persistence import MySQLClientPersistenceValidator
from validators.mysql_client.tests.unit.stubs import ConnStub, CursorStub
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

TEST_TOKEN = "test-token-abc123"

# Scope token for the default model UUID / relation id 0 / unit app/0 below.
SCOPE_TOKEN = "da7d88bc9ad4d4fd"
CANARY_PREFIX = f"validator_canary_{SCOPE_TOKEN}_"

VALID_DATABAG: dict[str, str] = {
    "endpoints": "10.1.2.3:3306",
    "database": "mydb",
    "username": "myuser",
    "password": "mypassword",
}


def _make_persistence_validator(
    databag: dict[str, str],
    endpoint: str = "database",
    role: RelationRoleStub = RelationRoleStub.requires,
    relation_id: int = 0,
    model_uuid: str = "11111111-1111-1111-1111-111111111111",
    unit_name: str = "app/0",
) -> MySQLClientPersistenceValidator:
    app = ApplicationStub()
    relation = RelationStub(app=app, data={app: databag}, name=endpoint, id=relation_id)
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(
            relation,
            interface_name="mysql_client",
            role=role,
            local_model_uuid=model_uuid,
            local_unit_name=unit_name,
        ),
    )
    return MySQLClientPersistenceValidator(charm, cast(ops.Relation, relation))


class TestMySQLClientPersistenceValidatorRole:
    @pytest.mark.parametrize("role", [RelationRoleStub.provides, RelationRoleStub.peer])
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


class TestMySQLClientPersistenceValidatorConnection:
    def test_prepare_raises_when_endpoints_is_blank(self) -> None:
        # GIVEN a databag with a present but blank "endpoints" field
        databag = {**VALID_DATABAG, "endpoints": ""}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="endpoints"):
            validator.prepare()

    def test_checkpoint_raises_when_endpoints_is_missing(self) -> None:
        # GIVEN a databag missing the "endpoints" field entirely
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "endpoints"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="endpoints"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

    def test_prepare_raises_when_first_endpoint_is_blank_after_split(self) -> None:
        # GIVEN a non-blank "endpoints" whose first entry is blank once stripped. Without this
        # guard PyMySQL receives an empty host, which it silently resolves to localhost.
        databag = {**VALID_DATABAG, "endpoints": " ,10.1.2.3:3306"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="endpoints"):
            validator.prepare()

    def test_enables_autocommit_before_writing(self) -> None:
        # GIVEN a canary write, which is useless if rolled back when the connection closes
        validator = _make_persistence_validator(VALID_DATABAG)
        conn = ConnStub()

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.prepare()

        # THEN
        assert conn.autocommit_calls == [True]


class TestMySQLClientPersistenceValidatorPrepare:
    def test_creates_canary_table_and_returns_state(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)
        conn = ConnStub()

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            state = validator.prepare()

        # THEN
        assert isinstance(state, PersistenceState)
        assert state.ref == 1
        assert state.token
        queries = " ".join(conn.cursor_stub.executed_queries)
        assert f"`{CANARY_PREFIX}{state.id:020d}`" in queries
        assert "DROP TABLE IF EXISTS" in queries
        assert "CREATE TABLE" in queries
        assert "INSERT INTO" in queries
        # The token is written as the row's marker, so checkpoint() can match on it later
        assert conn.cursor_stub.executed_params[-1][0] == state.token

    def test_generates_distinct_identifiers_across_calls(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=ConnStub()):
            # WHEN
            first = validator.prepare()
            second = validator.prepare()

        # THEN
        assert first.id != second.id
        assert first.token != second.token

    def test_is_repeatable_for_an_identifier_that_already_has_a_canary_table(self) -> None:
        # GIVEN two prepare() calls forced onto the same identifier (e.g. a re-run after a failed
        # prepare, or a resumed run). A plain CREATE TABLE would fail the second time, so prepare()
        # drops any existing table first and must still return usable state.
        validator = _make_persistence_validator(VALID_DATABAG)
        conn = ConnStub()

        with (
            patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn),
            patch("validators.mysql_client.persistence.uuid.uuid4") as mock_uuid,
        ):
            mock_uuid.return_value.int = 12345
            mock_uuid.return_value.hex = TEST_TOKEN
            first = validator.prepare()
            second = validator.prepare()

        # THEN both calls target the same table and return equivalent, usable state
        assert first == second
        assert second.ref == 1
        drops = [q for q in conn.cursor_stub.executed_queries if "DROP TABLE IF EXISTS" in q]
        assert len(drops) == 2
        assert all(f"`{CANARY_PREFIX}{12345:020d}`" in q for q in drops)


class TestMySQLClientPersistenceValidatorCheckpoint:
    def test_passes_when_row_count_matches_expected_ref(self) -> None:
        # GIVEN the canary table has exactly the expected number of rows with matching identity
        validator = _make_persistence_validator(VALID_DATABAG)
        # Schema-resolution query, then the id=checkpoint_ref identity-matching count query.
        cursor = CursorStub(fetchone_rows=[("mydb",), (2,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN
        assert result.status == "PASS"
        check = next(c for c in result.checks if c.name == "row_count")
        assert check.passed
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=3)
        # A new row is still written to continue the chain
        assert any("INSERT INTO" in q for q in cursor.executed_queries)

    def test_fails_when_row_count_is_lower_than_expected(self) -> None:
        # GIVEN data loss: fewer matching rows than expected
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchone_rows=[("mydb",), (1,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=7, ref=3))

        # THEN
        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "row_count")
        assert not check.passed
        assert "3" in check.message and "1" in check.message
        # ValidatorRunner only carries the returned state forward on PASS, so writing/advancing on
        # FAIL would drift the actual row count past what a later checkpoint can compare against.
        assert new_state == PersistenceState(token=TEST_TOKEN, id=7, ref=3)
        assert not any("INSERT INTO" in q for q in cursor.executed_queries)

    def test_fails_when_table_is_not_found(self) -> None:
        # GIVEN the canary table no longer exists (e.g. it was dropped, or never created)
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchone_rows=[None])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=7, ref=1))

        # THEN
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=7, ref=1)
        assert not any("INSERT INTO" in q for q in cursor.executed_queries)

    def test_uses_canary_table_name_from_expected_identifier(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchone_rows=[("mydb",), (1,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN
        assert cursor.executed_params[0] == (f"{CANARY_PREFIX}00000000000000000099",)
        assert any(f"`{CANARY_PREFIX}00000000000000000099`" in q for q in cursor.executed_queries)

    def test_scopes_table_lookup_to_the_connected_database(self) -> None:
        # GIVEN MySQL has no search_path: an unqualified CREATE TABLE always lands in the
        # connection's default database, so lookup is scoped to DATABASE() rather than every schema.
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchone_rows=[("mydb",), (1,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN the resolved schema qualifies the count query, so a same-named table in another
        # database can't be addressed instead
        lookup_query = next(q for q in cursor.executed_queries if "information_schema" in q)
        assert "table_schema = DATABASE()" in lookup_query
        count_query = next(q for q in cursor.executed_queries if "COUNT(*)" in q)
        assert "`mydb`." in count_query

    def test_filters_row_count_by_token(self) -> None:
        # GIVEN a bare count(*) would let a table recreated from scratch with an equally-sized set
        # of unrelated rows pass, so the count must be scoped to the random per-run token.
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchone_rows=[("mydb",), (1,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN both the count query and the follow-up insert are scoped to the same token
        count_index = next(i for i, q in enumerate(cursor.executed_queries) if "COUNT(*)" in q)
        insert_index = next(i for i, q in enumerate(cursor.executed_queries) if "INSERT INTO" in q)
        assert "WHERE marker = %s" in cursor.executed_queries[count_index]
        assert cursor.executed_params[count_index] == (TEST_TOKEN, 1)
        assert cursor.executed_params[insert_index][0] == TEST_TOKEN

    def test_fails_when_table_was_dropped_and_recreated_with_same_ref(self) -> None:
        # GIVEN a table dropped and recreated from scratch: AUTO_INCREMENT resets, so reinserting
        # rows with the same checkpoint_ref reproduces `id == checkpoint_ref` and the same row
        # count. Only the random per-run token distinguishes the original rows from the recreated
        # ones, so the token-scoped count matches nothing.
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchone_rows=[("mydb",), (0,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN the recreated table is detected as data loss, not a false PASS
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=2)
        assert not any("INSERT INTO" in q for q in cursor.executed_queries)

    def test_rejects_state_without_a_token(self) -> None:
        # GIVEN a state serialised before the token existed (or otherwise restored/malformed).
        # Matching on an empty marker would count rows carrying no token at all, silently degrading
        # the identity check. The base protocol rejects such a state at construction.
        with pytest.raises(ValidationError):
            PersistenceState(id=1, ref=1)

    def test_result_endpoint_and_interface_are_set(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG, endpoint="my-db")
        cursor = CursorStub(fetchone_rows=[("mydb",), (1,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            result, _ = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

        # THEN
        assert result.endpoint == "my-db"
        assert result.interface == "mysql_client"
        assert result.level == "deep"

    @pytest.mark.parametrize("identifier", [1 << 63, -1])
    def test_raises_when_expected_identifier_is_out_of_range(self, identifier: int) -> None:
        # GIVEN a restored/malformed PersistenceState with an id prepare() could not have produced.
        # Without this check it would be formatted into a table name, silently targeting the wrong
        # table instead of failing safely.
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.mysql_client.persistence.pymysql.connect") as mock_connect:
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=identifier, ref=1))

        # THEN the state is rejected before any read/write
        mock_connect.assert_not_called()

    @pytest.mark.parametrize("ref", [0, -1])
    def test_raises_when_expected_ref_is_out_of_range(self, ref: int) -> None:
        # GIVEN a restored/malformed PersistenceState with ref <= 0 (prepare() always returns
        # ref=1). An empty or partially recreated table (actual == 0) would otherwise satisfy
        # `actual == expected.ref` and report a false PASS.
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.mysql_client.persistence.pymysql.connect") as mock_connect:
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=ref))

        # THEN the state is rejected before any read/write
        mock_connect.assert_not_called()


class TestMySQLClientPersistenceValidatorCleanup:
    def test_drops_all_discovered_canary_tables(self) -> None:
        # GIVEN two leftover canary tables are discovered
        validator = _make_persistence_validator(VALID_DATABAG)
        table_1 = f"{CANARY_PREFIX}00000000000000000001"
        table_2 = f"{CANARY_PREFIX}00000000000000000002"
        cursor = CursorStub(fetchall_rows=[("mydb", table_1), ("mydb", table_2)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN each is dropped, schema-qualified and backtick-quoted (defence in depth: the names
        # come from information_schema, not user input, but should not be interpolated blindly)
        drop_queries = [q for q in cursor.executed_queries if "DROP TABLE" in q]
        assert any(table_1 in q for q in drop_queries)
        assert any(table_2 in q for q in drop_queries)
        assert all("`mydb`.`validator_canary_" in q for q in drop_queries)

    def test_is_a_noop_when_no_canary_tables_exist(self) -> None:
        # GIVEN nothing is discovered (e.g. prepare() never ran for this relation)
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        assert not any("DROP TABLE" in q for q in cursor.executed_queries)

    def test_scopes_discovery_to_the_connected_database(self) -> None:
        # GIVEN MySQL resolves an unqualified CREATE TABLE against the connection's default
        # database, so discovery beyond DATABASE() could only match another database's tables.
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        select_query = next(q for q in cursor.executed_queries if "information_schema" in q)
        assert "table_schema = DATABASE()" in select_query

    def test_restricts_discovery_to_base_tables(self) -> None:
        # GIVEN information_schema.tables also lists views. A view sharing the canary prefix would
        # make DROP TABLE fail and abort cleanup, leaving other discovered tables undropped.
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        select_query = next(q for q in cursor.executed_queries if "information_schema" in q)
        assert "table_type = 'BASE TABLE'" in select_query

    def test_escapes_like_wildcards_in_prefix_pattern(self) -> None:
        # GIVEN the canary prefix contains underscores, which are LIKE wildcards; an unescaped
        # pattern could match unrelated tables (e.g. "validatorXcanaryY...").
        validator = _make_persistence_validator(VALID_DATABAG)
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        select_query = next(q for q in cursor.executed_queries if "information_schema" in q)
        assert "ESCAPE '|'" in select_query
        assert cursor.executed_params[0] == (f"validator|_canary|_{SCOPE_TOKEN}|_%",)

    def test_scopes_discovery_to_this_relations_id(self) -> None:
        # GIVEN the bare `validator_canary_` prefix is shared by every relation, so matching on it
        # would let cleanup drop a concurrent relation's canary tables.
        validator = _make_persistence_validator(VALID_DATABAG, relation_id=7)
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        assert cursor.executed_params[0] == ("validator|_canary|_9c1f7f01a01956b9|_%",)

    def test_scopes_discovery_to_this_models_uuid(self) -> None:
        # GIVEN relation ids are per-model, so two models sharing a database can expose the same
        # relation id; discovery must not cross that boundary.
        validator = _make_persistence_validator(
            VALID_DATABAG, relation_id=7, model_uuid="22222222-2222-2222-2222-222222222222"
        )
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        assert cursor.executed_params[0] != ("validator|_canary|_9c1f7f01a01956b9|_%",)

    def test_scopes_discovery_to_this_unit(self) -> None:
        # GIVEN the runner runs persistence validators on every unit, so two units of one
        # application share model.uuid and relation_id while owning separate canary tables.
        validator = _make_persistence_validator(VALID_DATABAG, relation_id=7, unit_name="app/0")
        other_unit = _make_persistence_validator(VALID_DATABAG, relation_id=7, unit_name="app/1")

        # WHEN / THEN
        assert other_unit._canary_table_prefix() != validator._canary_table_prefix()

    def test_rejects_discovered_tables_that_only_share_the_prefix(self) -> None:
        # GIVEN the LIKE query only narrows candidates by prefix, so a same-prefixed but unrelated
        # table (e.g. a hand-created backup) must be re-checked against the exact name shape.
        validator = _make_persistence_validator(VALID_DATABAG)
        good_table = f"{CANARY_PREFIX}00000000000000000001"
        look_alike = f"{CANARY_PREFIX}backup"
        cursor = CursorStub(fetchall_rows=[("mydb", good_table), ("mydb", look_alike)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        drop_queries = [q for q in cursor.executed_queries if "DROP TABLE" in q]
        assert any(good_table in q for q in drop_queries)
        assert not any(look_alike in q for q in drop_queries)

    def test_rejects_discovered_tables_with_an_out_of_range_identifier(self) -> None:
        # GIVEN a 20-digit suffix can encode a value far larger than the 63-bit maximum prepare()
        # can produce, so shape alone isn't enough to prove we created it.
        validator = _make_persistence_validator(VALID_DATABAG)
        good_table = f"{CANARY_PREFIX}00000000000000000001"
        out_of_range_table = f"{CANARY_PREFIX}99999999999999999999"
        cursor = CursorStub(fetchall_rows=[("mydb", good_table), ("mydb", out_of_range_table)])
        conn = ConnStub(cursor_stub=cursor)

        with patch("validators.mysql_client.persistence.pymysql.connect", return_value=conn):
            # WHEN
            validator.cleanup()

        # THEN
        drop_queries = [q for q in cursor.executed_queries if "DROP TABLE" in q]
        assert any(good_table in q for q in drop_queries)
        assert not any(out_of_range_table in q for q in drop_queries)

    def test_raises_not_applicable_when_no_credentials_present(self) -> None:
        # GIVEN a databag without any credential fields (e.g. the relation is already gone)
        validator = _make_persistence_validator({})

        with patch("validators.mysql_client.persistence.pymysql.connect") as mock_connect:
            # WHEN / THEN cleanup is skipped rather than reporting a success that removed nothing
            with pytest.raises(PersistenceNotApplicable):
                validator.cleanup()

        # THEN no connection was attempted
        mock_connect.assert_not_called()

    def test_raises_not_applicable_when_credentials_are_incomplete(self) -> None:
        # GIVEN a relation with "endpoints" but not yet database/username/password (mid-setup)
        validator = _make_persistence_validator({"endpoints": "10.1.2.3:3306"})

        with patch("validators.mysql_client.persistence.pymysql.connect") as mock_connect:
            # WHEN / THEN
            with pytest.raises(PersistenceNotApplicable):
                validator.cleanup()

        # THEN no connection was attempted
        mock_connect.assert_not_called()
