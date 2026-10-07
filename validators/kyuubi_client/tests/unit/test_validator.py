# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import sqlite3
import ssl
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
    "username": "canary-user",
    "password": "canary-password",
}


@dataclass
class CursorStub:
    fetchall_rows: list[tuple[Any, ...]] = field(default_factory=list)
    fetchone_rows: list[tuple[Any, ...] | None] = field(default_factory=list)
    executed_queries: list[str] = field(default_factory=list, init=False)
    executed_parameters: list[tuple[str | int, ...] | None] = field(default_factory=list, init=False)
    _fetchone_index: int = field(default=0, init=False)

    def execute(self, query: str, parameters: tuple[str | int, ...] | None = None) -> None:
        self.executed_queries.append(query)
        self.executed_parameters.append(parameters)

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
    secrets: dict[str, dict[str, str]] | None = None,
) -> KyuubiClientPersistenceValidator:
    app = ApplicationStub()
    relation = RelationStub(name="kyuubi", id=relation_id, app=app, data={app: databag or VALID_DATABAG})
    charm = make_charm_from_relation(
        relation,
        interface_name="kyuubi_client",
        role=role,
        local_unit_name=unit_name,
    )
    charm.model._secrets = secrets or {}
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
        assert conn.cursor_stub.executed_parameters[-1] == (state.token, 1)
        assert conn.cursor_stub.executed_queries[-1].endswith("VALUES (%s, %s)")
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
    def test_rejects_ldap_placeholders_without_connecting(self, placeholder: str) -> None:
        validator = _make_validator(
            databag={
                "uris": "jdbc:kyuubi://kyuubi.example:10010/test_db",
                "database": "test_db",
                "username": placeholder,
                "password": placeholder,
            }
        )

        with patch("validators.kyuubi_client.validator.hive.Connection") as connection:
            with pytest.raises(RuntimeError, match="LDAP placeholders"):
                validator._open_connection()
        connection.assert_not_called()

    def test_connects_with_custom_authentication(self) -> None:
        validator = _make_validator(
            databag={
                **VALID_DATABAG,
                "username": "canary-user",
                "password": "canary-password",
            }
        )

        with (
            patch("validators.kyuubi_client.validator.TSocket.TSocket") as socket,
            patch("validators.kyuubi_client.validator.hive.Connection") as connection,
        ):
            validator._open_connection()

        socket.assert_called_once_with("kyuubi.example", 10009)
        socket.return_value.setTimeout.assert_called_once_with(5000)
        options = connection.call_args.kwargs
        assert options.keys() == {"database", "username", "thrift_transport"}
        assert options["database"] == "test_db"
        assert options["username"] == "canary-user"
        sasl = options["thrift_transport"].sasl_client_factory()
        assert sasl.start("PLAIN") == (True, "PLAIN", b"\x00canary-user\x00canary-password")

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
            with pytest.raises(RuntimeError, match="usable username and password"):
                validator._open_connection()
        connection.assert_not_called()

    def test_preserves_nonblank_credentials_verbatim(self) -> None:
        # GIVEN credentials whose whitespace is part of the value, not a placeholder.
        validator = _make_validator(databag={**VALID_DATABAG, "username": " user ", "password": " secret "})

        # WHEN / THEN nonblank credentials are not trimmed.
        config = validator._connection_config()
        assert config.username == " user "
        assert config.password == " secret "

    def test_authenticated_options_pass_pyhive_argument_validation(self) -> None:
        # GIVEN supplied credentials and the real PyHive constructor.
        validator = _make_validator()

        # WHEN / THEN PyHive accepts CUSTOM with supplied credentials.
        with patch("thrift_sasl.TSaslClientTransport.open", side_effect=RuntimeError("transport reached")):
            with pytest.raises(RuntimeError, match="transport reached"):
                validator._open_connection()

    @pytest.mark.parametrize("missing_field", ["username", "password"])
    def test_rejects_missing_credentials(self, missing_field: str) -> None:
        databag = dict(VALID_DATABAG)
        del databag[missing_field]
        validator = _make_validator(databag=databag)

        with patch("validators.kyuubi_client.validator.hive.Connection") as connection:
            with pytest.raises(RuntimeError, match="usable username and password"):
                validator._open_connection()
        connection.assert_not_called()


class TestTLS:
    def test_uses_verified_tls_and_sasl_with_secret_credentials(self) -> None:
        validator = _make_validator(
            databag={
                "endpoints": "kyuubi.example:10010",
                "database": "test_db",
                "secret-user": "secret:user",
                "secret-tls": "secret:tls",
            },
            secrets={
                "secret:user": {"username": "canary-user", "password": "canary-password"},
                "secret:tls": {"tls": "True", "tls-ca": "test-ca"},
            },
        )

        with (
            patch("ssl.SSLContext.load_verify_locations") as load_ca,
            patch("validators.kyuubi_client.validator._TLSSocket") as socket,
            patch("validators.kyuubi_client.validator.hive.Connection") as connection,
        ):
            validator._open_connection()

        load_ca.assert_called_once_with(None, None, "test-ca")
        context = socket.call_args.kwargs["ssl_context"]
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
        assert socket.call_args.args == ("kyuubi.example", 10010)
        socket.return_value.setTimeout.assert_called_once_with(5000)
        actual_transport = connection.call_args.kwargs["thrift_transport"]
        assert actual_transport.mechanism == "PLAIN"
        sasl = actual_transport.sasl_client_factory()
        success, mechanism, response = sasl.start("PLAIN")
        assert success
        assert mechanism == "PLAIN"
        assert response == b"\x00canary-user\x00canary-password"
        assert connection.call_args.kwargs.keys() == {"database", "username", "thrift_transport"}
        assert connection.call_args.kwargs["database"] == "test_db"

    @pytest.mark.parametrize("tls_ca", [None, "", " "], ids=["absent", "empty", "blank"])
    def test_rejects_missing_ca_before_connecting(self, tls_ca: str | None) -> None:
        databag = {**VALID_DATABAG, "tls": "True"}
        if tls_ca is not None:
            databag["tls-ca"] = tls_ca
        validator = _make_validator(databag=databag)

        with patch("validators.kyuubi_client.validator.hive.Connection") as connection:
            with pytest.raises(RuntimeError, match="'tls-ca' is missing"):
                validator._open_connection()
        connection.assert_not_called()

    def test_rejects_invalid_ca_before_connecting(self) -> None:
        validator = _make_validator(databag={**VALID_DATABAG, "tls": "True", "tls-ca": "not a certificate"})

        with patch("validators.kyuubi_client.validator.hive.Connection") as connection:
            with pytest.raises(ssl.SSLError):
                validator._open_connection()
        connection.assert_not_called()

    def test_rejects_invalid_tls_flag(self) -> None:
        validator = _make_validator(databag={**VALID_DATABAG, "tls": "invalid"})
        with pytest.raises(RuntimeError, match="'tls' must be True or False"):
            validator._open_connection()

    def test_false_tls_flag_ignores_stale_ca(self) -> None:
        validator = _make_validator(databag={**VALID_DATABAG, "tls": "False", "tls-ca": "stale-ca"})
        with (
            patch("validators.kyuubi_client.validator._TLSSocket") as tls_socket,
            patch("validators.kyuubi_client.validator.TSocket.TSocket") as plain_socket,
            patch("validators.kyuubi_client.validator.hive.Connection"),
        ):
            validator._open_connection()
        tls_socket.assert_not_called()
        plain_socket.assert_called_once_with("kyuubi.example", 10009)

    def test_advertised_ca_does_not_load_platform_roots(self) -> None:
        # GIVEN an advertised CA and the real SSL context constructor.
        validator = _make_validator(databag={**VALID_DATABAG, "tls": "True", "tls-ca": "test-ca"})
        with (
            patch("ssl.SSLContext.load_verify_locations") as load_ca,
            patch("ssl.SSLContext.load_default_certs") as load_defaults,
            patch("validators.kyuubi_client.validator._TLSSocket") as socket,
            patch("validators.kyuubi_client.validator.hive.Connection"),
        ):
            validator._open_connection()

        # THEN explicit cadata loads only the relation CA, not the default trust store.
        load_ca.assert_called_once_with(None, None, "test-ca")
        load_defaults.assert_not_called()
        context = socket.call_args.kwargs["ssl_context"]
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname

    @pytest.mark.parametrize("tls", ["False", "True"], ids=["plain", "tls"])
    def test_real_socket_has_bounded_connect_and_io_timeout(self, tls: str) -> None:
        validator = _make_validator(databag={**VALID_DATABAG, "tls": tls, "tls-ca": "test-ca"})
        with (
            patch("ssl.SSLContext.load_verify_locations"),
            patch("validators.kyuubi_client.validator.hive.Connection") as connection,
        ):
            validator._open_connection()

        transport = connection.call_args.kwargs["thrift_transport"]
        assert transport._trans._timeout == 5

    def test_tls_options_pass_real_pyhive_and_socket_argument_validation(self) -> None:
        validator = _make_validator(databag={**VALID_DATABAG, "tls": "True", "tls-ca": "test-ca"})

        with (
            patch("ssl.SSLContext.load_verify_locations"),
            patch("thrift_sasl.TSaslClientTransport.open", side_effect=RuntimeError("transport reached")),
        ):
            with pytest.raises(RuntimeError, match="transport reached"):
                validator._open_connection()


class TestCheckpoint:
    def test_passes_and_advances_state_when_count_matches(self) -> None:
        validator = _make_validator()
        table_name = f"{validator._canary_table_prefix()}{42:020d}"
        cursor = CursorStub(fetchall_rows=[(table_name,)], fetchone_rows=[(2, 2)])
        conn = ConnStub(cursor_stub=cursor)

        with patch.object(validator, "_open_connection", return_value=conn):
            result, new_state = validator.checkpoint(PersistenceState(id=42, ref=2, token=TEST_TOKEN))

        assert result.status == "PASS"
        assert new_state == PersistenceState(id=42, ref=3, token=TEST_TOKEN)
        count_query = next(query for query in cursor.executed_queries if query.startswith("SELECT COUNT(*)"))
        assert count_query.endswith("WHERE marker = %s")
        assert cursor.executed_parameters[1] == (2, TEST_TOKEN)
        assert "checkpoint_ref BETWEEN 1 AND %s" in count_query
        assert cursor.executed_queries[-1].endswith("VALUES (%s, %s)")
        assert cursor.executed_parameters[-1] == (TEST_TOKEN, 3)

    def test_fails_without_writing_when_count_does_not_match(self) -> None:
        validator = _make_validator()
        table_name = f"{validator._canary_table_prefix()}{42:020d}"
        cursor = CursorStub(fetchall_rows=[(table_name,)], fetchone_rows=[(1, 1)])
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
        cursor = CursorStub(fetchall_rows=[(table_name,)], fetchone_rows=[(0, 0)])
        conn = ConnStub(cursor_stub=cursor)
        expected = PersistenceState(id=42, ref=1, token=TEST_TOKEN)

        with patch.object(validator, "_open_connection", return_value=conn):
            result, new_state = validator.checkpoint(expected)

        assert result.status == "FAIL"
        assert new_state == expected
        count_query = next(query for query in cursor.executed_queries if query.startswith("SELECT COUNT(*)"))
        assert count_query.endswith("WHERE marker = %s")
        assert cursor.executed_parameters[1] == (1, TEST_TOKEN)
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

    @pytest.mark.parametrize(
        "token", ["quote'token", "backslash\\'token", "line\nbreak"], ids=["quote", "backslash", "newline"]
    )
    def test_binds_restored_token_for_both_read_and_write(self, token: str) -> None:
        validator = _make_validator()
        table_name = validator._canary_table_name(42)
        cursor = CursorStub(fetchall_rows=[(table_name,)], fetchone_rows=[(1, 1)])
        conn = ConnStub(cursor_stub=cursor)

        with patch.object(validator, "_open_connection", return_value=conn):
            result, new_state = validator.checkpoint(PersistenceState(id=42, ref=1, token=token))

        assert result.status == "PASS"
        assert new_state.token == token
        assert cursor.executed_parameters == [None, (1, token), (token, 2)]
        assert all(token not in query for query in cursor.executed_queries)

    @dataclass(frozen=True)
    class ReferenceParams:
        refs: tuple[int | None, ...]
        passed: bool

    @pytest.mark.parametrize(
        "params",
        [
            ReferenceParams((1, 2, 3), True),
            ReferenceParams((3, 1, 2), True),
            ReferenceParams((1, 1, 3), False),
            ReferenceParams((1, 2, 4), False),
            ReferenceParams((0, 2, 3), False),
            ReferenceParams((1, 2, None), False),
            ReferenceParams((1, 3), False),
            ReferenceParams((1, 2, 3, 4), False),
        ],
        ids=["contiguous", "reordered", "duplicate", "above-range", "below-range", "null", "missing", "extra"],
    )
    def test_verifies_actual_reference_set(self, params: ReferenceParams) -> None:
        # GIVEN token-bearing rows whose count alone may conceal reference corruption.
        validator = _make_validator()
        table_name = validator._canary_table_name(42)
        cursor = CursorStub(fetchall_rows=[(table_name,)])
        conn = ConnStub(cursor_stub=cursor)
        expected = PersistenceState(id=42, ref=3, token=TEST_TOKEN)
        with sqlite3.connect(":memory:") as database:
            database.execute(f"CREATE TABLE `{table_name}` (marker TEXT, checkpoint_ref INTEGER)")
            database.executemany(
                f"INSERT INTO `{table_name}` VALUES (?, ?)", [(TEST_TOKEN, ref) for ref in params.refs]
            )
            original_execute = cursor.execute

            def execute(query: str, parameters: tuple[str | int, ...] | None = None) -> None:
                original_execute(query, parameters)
                if query.startswith("SELECT"):
                    cursor.fetchone_rows.append(database.execute(query.replace("%s", "?"), parameters or ()).fetchone())

            # WHEN the real aggregate SQL runs against the seeded reference set.
            with (
                patch.object(cursor, "execute", side_effect=execute),
                patch.object(validator, "_open_connection", return_value=conn),
            ):
                result, state = validator.checkpoint(expected)

        # THEN corrupt sets fail without writes or state advancement.
        assert result.status == ("PASS" if params.passed else "FAIL")
        assert state == (PersistenceState(id=42, ref=4, token=TEST_TOKEN) if params.passed else expected)
        assert any(query.startswith("INSERT") for query in cursor.executed_queries) == params.passed
        assert conn.closed


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
