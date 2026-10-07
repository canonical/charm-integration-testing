# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import pytest
from pydantic import ValidationError

from validators.base import PersistenceNotApplicable, PersistenceState
from validators.kyuubi_client.validator import KyuubiClientPersistenceValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import ApplicationStub, RelationRoleStub, RelationStub

TEST_TOKEN = "test-token-abc123"
VALID_DATABAG = {
    "uris": "jdbc:kyuubi://kyuubi.example:10009/test_db",
    "database": "test_db",
}


@dataclass
class CursorStub:
    fetchall_rows: list[tuple[Any, ...]] = field(default_factory=list)
    fetchone_rows: list[tuple[Any, ...] | None] = field(default_factory=list)
    executed_queries: list[str] = field(default_factory=list, init=False)
    _fetchone_index: int = field(default=0, init=False)

    def execute(self, query: str) -> None:
        self.executed_queries.append(query)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.fetchall_rows

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._fetchone_index >= len(self.fetchone_rows):
            return None
        row = self.fetchone_rows[self._fetchone_index]
        self._fetchone_index += 1
        return row

    def __enter__(self) -> "CursorStub":
        return self

    def __exit__(self, *args: object) -> None:
        pass


@dataclass
class ConnStub:
    cursor_stub: CursorStub = field(default_factory=CursorStub)
    closed: bool = False

    def cursor(self) -> CursorStub:
        return self.cursor_stub

    def close(self) -> None:
        self.closed = True


def _make_validator(
    *,
    databag: dict[str, str] | None = None,
    role: RelationRoleStub = RelationRoleStub.requires,
    relation_id: int = 7,
    unit_name: str = "app/0",
) -> KyuubiClientPersistenceValidator:
    app = ApplicationStub()
    relation = RelationStub(name="kyuubi", id=relation_id, app=app, data={app: databag or VALID_DATABAG})
    charm = make_charm_from_relation(
        relation,
        interface_name="kyuubi_client",
        role=role,
        local_unit_name=unit_name,
    )
    return KyuubiClientPersistenceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


class TestRoleGating:
    @pytest.mark.parametrize("role", [RelationRoleStub.provides, RelationRoleStub.peer])
    def test_lifecycle_methods_raise_not_applicable(self, role: RelationRoleStub) -> None:
        validator = _make_validator(role=role)

        with pytest.raises(PersistenceNotApplicable):
            validator.prepare()
        with pytest.raises(PersistenceNotApplicable):
            validator.checkpoint(PersistenceState(id=1, ref=1, token=TEST_TOKEN))
        with pytest.raises(PersistenceNotApplicable):
            validator.cleanup()


class TestPrepare:
    def test_creates_canary_table_and_returns_initial_state(self) -> None:
        validator = _make_validator()
        conn = ConnStub()

        with patch.object(validator, "_open_connection", return_value=conn):
            state = validator.prepare()

        table = f"`{validator._canary_table_prefix()}{state.id:020d}`"
        assert isinstance(state, PersistenceState)
        assert state.ref == 1
        assert state.token
        assert table in " ".join(conn.cursor_stub.executed_queries)
        assert any(query.startswith("CREATE TABLE") for query in conn.cursor_stub.executed_queries)
        assert any("INSERT INTO TABLE" in query and state.token in query for query in conn.cursor_stub.executed_queries)
        assert conn.closed

    def test_repeated_prepare_with_same_identifier_resets_canary(self) -> None:
        validator = _make_validator()
        conn = ConnStub()

        with (
            patch.object(validator, "_open_connection", return_value=conn),
            patch("validators.kyuubi_client.validator.uuid.uuid4") as uuid4,
        ):
            uuid4.return_value.int = 42
            uuid4.return_value.hex = TEST_TOKEN
            first = validator.prepare()
            second = validator.prepare()

        assert first == second
        assert first.ref == 1
        assert sum(query.startswith("DROP TABLE IF EXISTS") for query in conn.cursor_stub.executed_queries) == 2

    @pytest.mark.parametrize("placeholder", ["", " ", "\t\n"], ids=["empty", "space", "whitespace"])
    def test_connects_using_jdbc_uri_without_authentication(self, placeholder: str) -> None:
        validator = _make_validator(
            databag={
                "uris": "jdbc:kyuubi://kyuubi.example:10010/test_db",
                "database": "test_db",
                "username": placeholder,
                "password": placeholder,
            }
        )

        with patch("validators.kyuubi_client.validator.hive.Connection") as connection:
            validator._open_connection()

        connection.assert_called_once_with(
            host="kyuubi.example",
            port=10010,
            database="test_db",
            username="validator",
            password=None,
            auth="NONE",
        )

    def test_connects_with_ldap_authentication(self) -> None:
        validator = _make_validator(
            databag={
                **VALID_DATABAG,
                "username": "canary-user",
                "password": "canary-password",
            }
        )

        with patch("validators.kyuubi_client.validator.hive.Connection") as connection:
            validator._open_connection()

        connection.assert_called_once_with(
            host="kyuubi.example",
            port=10009,
            database="test_db",
            username="canary-user",
            password="canary-password",
            auth="LDAP",
        )

    @pytest.mark.parametrize(
        ("username", "password"),
        [("", "secret"), (" ", "secret"), ("user", ""), ("user", " ")],
        ids=["missing-username", "blank-username", "missing-password", "blank-password"],
    )
    def test_rejects_one_sided_credentials(self, username: str, password: str) -> None:
        # GIVEN a genuinely incomplete credential pair.
        validator = _make_validator(databag={**VALID_DATABAG, "username": username, "password": password})

        # WHEN / THEN no connection is attempted.
        with patch("validators.kyuubi_client.validator.hive.Connection") as connection:
            with pytest.raises(RuntimeError, match="provided together"):
                validator._open_connection()
        connection.assert_not_called()

    def test_preserves_nonblank_credentials_verbatim(self) -> None:
        # GIVEN credentials whose whitespace is part of the value, not a placeholder.
        validator = _make_validator(databag={**VALID_DATABAG, "username": " user ", "password": " secret "})

        # WHEN / THEN only whitespace-only credentials are normalized.
        config = validator._connection_config()
        assert config["username"] == " user "
        assert config["password"] == " secret "
        assert config["auth"] == "LDAP"

    def test_unauthenticated_options_pass_pyhive_argument_validation(self) -> None:
        # GIVEN provider placeholders and the real PyHive constructor.
        validator = _make_validator(databag={**VALID_DATABAG, "username": " ", "password": " "})

        # WHEN / THEN PyHive reaches transport opening rather than rejecting NONE with a password.
        with patch("thrift_sasl.TSaslClientTransport.open", side_effect=RuntimeError("transport reached")):
            with pytest.raises(RuntimeError, match="transport reached"):
                validator._open_connection()


class TestCheckpoint:
    def test_passes_and_advances_state_when_count_matches(self) -> None:
        validator = _make_validator()
        table_name = f"{validator._canary_table_prefix()}{42:020d}"
        cursor = CursorStub(fetchall_rows=[(table_name,)], fetchone_rows=[(2,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch.object(validator, "_open_connection", return_value=conn):
            result, new_state = validator.checkpoint(PersistenceState(id=42, ref=2, token=TEST_TOKEN))

        assert result.status == "PASS"
        assert new_state == PersistenceState(id=42, ref=3, token=TEST_TOKEN)
        count_query = next(query for query in cursor.executed_queries if query.startswith("SELECT COUNT(*)"))
        assert f"WHERE marker = '{TEST_TOKEN}'" in count_query
        assert "BETWEEN" not in count_query
        assert any("VALUES ('test-token-abc123', 3)" in query for query in cursor.executed_queries)

    def test_fails_without_writing_when_count_does_not_match(self) -> None:
        validator = _make_validator()
        table_name = f"{validator._canary_table_prefix()}{42:020d}"
        cursor = CursorStub(fetchall_rows=[(table_name,)], fetchone_rows=[(1,)])
        conn = ConnStub(cursor_stub=cursor)
        expected = PersistenceState(id=42, ref=2, token=TEST_TOKEN)

        with patch.object(validator, "_open_connection", return_value=conn):
            result, new_state = validator.checkpoint(expected)

        assert result.status == "FAIL"
        assert new_state == expected
        assert not any("INSERT INTO TABLE" in query for query in cursor.executed_queries)

    def test_fails_when_table_was_recreated_without_the_original_token(self) -> None:
        validator = _make_validator()
        table_name = f"{validator._canary_table_prefix()}{42:020d}"
        cursor = CursorStub(fetchall_rows=[(table_name,)], fetchone_rows=[(0,)])
        conn = ConnStub(cursor_stub=cursor)
        expected = PersistenceState(id=42, ref=1, token=TEST_TOKEN)

        with patch.object(validator, "_open_connection", return_value=conn):
            result, new_state = validator.checkpoint(expected)

        assert result.status == "FAIL"
        assert new_state == expected
        count_query = next(query for query in cursor.executed_queries if query.startswith("SELECT COUNT(*)"))
        assert f"WHERE marker = '{TEST_TOKEN}'" in count_query
        assert not any("INSERT INTO TABLE" in query for query in cursor.executed_queries)

    def test_fails_when_table_is_missing(self) -> None:
        validator = _make_validator()
        cursor = CursorStub(fetchall_rows=[])
        conn = ConnStub(cursor_stub=cursor)
        expected = PersistenceState(id=42, ref=1, token=TEST_TOKEN)

        with patch.object(validator, "_open_connection", return_value=conn):
            result, new_state = validator.checkpoint(expected)

        assert result.status == "FAIL"
        assert new_state == expected
        assert not any(query.startswith("SELECT COUNT(*)") for query in cursor.executed_queries)

    @pytest.mark.parametrize("identifier", [-1, 1 << 63])
    def test_rejects_out_of_range_identifier_before_connecting(self, identifier: int) -> None:
        validator = _make_validator()

        with patch.object(validator, "_open_connection") as open_connection:
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(id=identifier, ref=1, token=TEST_TOKEN))

        open_connection.assert_not_called()

    @pytest.mark.parametrize("ref", [0, -1, 1 << 63, (1 << 63) - 1])
    def test_rejects_out_of_range_ref_before_connecting(self, ref: int) -> None:
        validator = _make_validator()

        with patch.object(validator, "_open_connection") as open_connection:
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(id=1, ref=ref, token=TEST_TOKEN))

        open_connection.assert_not_called()

    def test_rejects_state_without_token(self) -> None:
        with pytest.raises(ValidationError):
            PersistenceState(id=1, ref=1)


class TestCleanup:
    def test_drops_discovered_canary_tables_and_ignores_unrelated_tables(self) -> None:
        validator = _make_validator()
        good_table = f"{validator._canary_table_prefix()}{1:020d}"
        backup_table = f"{validator._canary_table_prefix()}backup"
        out_of_range_table = f"{validator._canary_table_prefix()}{(1 << 63):020d}"
        cursor = CursorStub(fetchall_rows=[(good_table,), (backup_table,), (out_of_range_table,)])
        conn = ConnStub(cursor_stub=cursor)

        with patch.object(validator, "_open_connection", return_value=conn):
            validator.cleanup()

        drops = [query for query in cursor.executed_queries if query.startswith("DROP TABLE")]
        assert drops == [f"DROP TABLE IF EXISTS `{good_table}`"]
        assert conn.closed

    def test_is_a_noop_when_no_canary_tables_exist(self) -> None:
        validator = _make_validator()
        conn = ConnStub(cursor_stub=CursorStub(fetchall_rows=[]))

        with patch.object(validator, "_open_connection", return_value=conn):
            validator.cleanup()

        assert not any("DROP TABLE" in query for query in conn.cursor_stub.executed_queries)

    def test_escapes_discovered_table_identifier(self) -> None:
        validator = _make_validator()
        table_name = f"{validator._canary_table_prefix()}{1:020d}"
        conn = ConnStub(cursor_stub=CursorStub(fetchall_rows=[(table_name,)]))

        with patch.object(validator, "_open_connection", return_value=conn):
            validator.cleanup()

        assert f"DROP TABLE IF EXISTS `{table_name}`" in conn.cursor_stub.executed_queries
