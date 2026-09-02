# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from typing import cast
from unittest.mock import patch

import ops
import pytest

from validators.test_utils.helpers import make_charm_from_relation, make_charm_from_relation_and_secrets
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)
from validators.trino_client.validator import TrinoClientValidator

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _make_validator(
    databag: dict[str, str],
    endpoint: str = "trino",
    role: RelationRoleStub = RelationRoleStub.requires,
    secrets: dict[str, dict[str, str]] | None = None,
) -> TrinoClientValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    if secrets:
        charm = make_charm_from_relation_and_secrets(relation, secrets, role=role)
        charm.meta.relations[endpoint].interface_name = "trino_client"
    else:
        charm = make_charm_from_relation(relation, interface_name="trino_client", role=role)
    return TrinoClientValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


@dataclass
class CursorStub:
    """Minimal cursor context manager; raises execute_error if set."""

    execute_error: Exception | None = None
    fetchall_rows: list[tuple[object, ...]] = field(default_factory=list)
    fetchone_row: tuple[object, ...] | None = (1,)

    def execute(self, query: str) -> None:
        if self.execute_error:
            raise self.execute_error

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.fetchall_rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self.fetchone_row

    def __enter__(self) -> "CursorStub":
        return self

    def __exit__(self, *args: object) -> None:
        pass


@dataclass
class ConnStub:
    """Minimal connection stub; cursor_stub is returned by cursor()."""

    cursor_stub: CursorStub = field(default_factory=CursorStub)
    closed: bool = field(default=False, init=False)

    def cursor(self) -> CursorStub:
        return self.cursor_stub

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "discovery-uri": "http://trino-coordinator.testing.svc.cluster.local:8080",
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTrinoClientValidatorSimple:
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

        # WHEN (mock connect so a "requires" role does not attempt a real network call)
        with patch("validators.trino_client.validator.trino.dbapi.connect", return_value=ConnStub()):
            result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

    def test_returns_error_when_no_remote_app(self) -> None:
        # GIVEN a relation with no remote application data
        app = ApplicationStub()
        relation = RelationStub(name="trino", id=0, app=app, data={})
        relation.data.clear()
        charm = make_charm_from_relation(relation, interface_name="trino_client")
        validator = TrinoClientValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"
        assert result.error is not None

    def test_fails_schema_check_when_discovery_uri_missing(self) -> None:
        # GIVEN a databag missing the required discovery-uri field
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "discovery-uri" in schema_check.message

    def test_fails_discovery_uri_check_when_no_hostname(self) -> None:
        # GIVEN a discovery-uri with no hostname
        validator = _make_validator({"discovery-uri": "http://"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        uri_check = next(c for c in result.checks if c.name == "discovery_uri")
        assert not uri_check.passed

    def test_passes_and_connects_anonymously_without_credentials(self) -> None:
        # GIVEN a valid databag with no credentials secret
        validator = _make_validator(VALID_DATABAG)
        conn = ConnStub()

        with patch("validators.trino_client.validator.trino.dbapi.connect", return_value=conn) as mock_connect:
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert connect_check.passed
        assert mock_connect.call_args.kwargs["user"] == "charm-integration-testing-validator"
        assert "auth" not in mock_connect.call_args.kwargs
        assert conn.closed

    def test_passes_and_connects_with_resolved_secret_credentials(self) -> None:
        # GIVEN a databag pointing at a Juju secret with username/password
        databag = {**VALID_DATABAG, "user-secret-id": "secret:abc"}
        validator = _make_validator(databag, secrets={"secret:abc": {"username": "alice", "password": "s3cr3t"}})
        conn = ConnStub()
        auth_sentinel = object()

        with (
            patch("validators.trino_client.validator.trino.dbapi.connect", return_value=conn) as mock_connect,
            patch(
                "validators.trino_client.validator.trino.auth.BasicAuthentication", return_value=auth_sentinel
            ) as mock_auth,
        ):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        assert mock_connect.call_args.kwargs["user"] == "alice"
        assert mock_connect.call_args.kwargs["http_scheme"] == "http"
        mock_auth.assert_called_once_with("alice", "s3cr3t")
        assert mock_connect.call_args.kwargs["auth"] is auth_sentinel

    def test_ignores_plaintext_databag_credentials_without_secret_id(self) -> None:
        # GIVEN a databag that contains plaintext credentials but no user-secret-id
        validator = _make_validator({**VALID_DATABAG, "username": "alice", "password": "s3cr3t"})
        conn = ConnStub()

        with patch("validators.trino_client.validator.trino.dbapi.connect", return_value=conn) as mock_connect:
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        assert mock_connect.call_args.kwargs["user"] == "charm-integration-testing-validator"
        assert "auth" not in mock_connect.call_args.kwargs

    def test_fails_connect_check_when_only_username_present(self) -> None:
        # GIVEN a secret with a username but no password
        databag = {**VALID_DATABAG, "user-secret-id": "secret:abc"}
        validator = _make_validator(databag, secrets={"secret:abc": {"username": "alice"}})

        with patch("validators.trino_client.validator.trino.dbapi.connect") as mock_connect:
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "Incomplete credentials" in connect_check.message
        mock_connect.assert_not_called()

    def test_fails_connect_check_when_only_password_present(self) -> None:
        # GIVEN a secret with a password but no username
        databag = {**VALID_DATABAG, "user-secret-id": "secret:abc"}
        validator = _make_validator(databag, secrets={"secret:abc": {"password": "s3cr3t"}})

        with patch("validators.trino_client.validator.trino.dbapi.connect") as mock_connect:
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "Incomplete credentials" in connect_check.message
        mock_connect.assert_not_called()

    def test_respects_https_scheme_from_discovery_uri(self) -> None:
        # GIVEN a valid https discovery URI and credentials
        databag = {
            "discovery-uri": "https://trino-coordinator.testing.svc.cluster.local:8443",
            "user-secret-id": "secret:abc",
        }
        validator = _make_validator(databag, secrets={"secret:abc": {"username": "alice", "password": "s3cr3t"}})
        conn = ConnStub()

        with patch("validators.trino_client.validator.trino.dbapi.connect", return_value=conn) as mock_connect:
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        assert mock_connect.call_args.kwargs["http_scheme"] == "https"

    def test_fails_connect_check_when_secret_resolution_raises(self) -> None:
        # GIVEN credential resolution that fails
        validator = _make_validator({**VALID_DATABAG, "user-secret-id": "secret:missing"})

        with patch.object(TrinoClientValidator, "_resolve_credentials", side_effect=RuntimeError("secret error")):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "secret error" in connect_check.message

    def test_fails_connect_check_when_cluster_unreachable(self) -> None:
        # GIVEN a valid databag but a coordinator that refuses connections
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.trino_client.validator.trino.dbapi.connect",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "Connection refused" in connect_check.message

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-endpoint")

        with patch("validators.trino_client.validator.trino.dbapi.connect", return_value=ConnStub()):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-endpoint"
        assert result.interface == "trino_client"


class TestTrinoClientValidatorDeep:
    def test_passes_canary_query(self) -> None:
        # GIVEN a valid databag and a coordinator that returns SELECT 1
        validator = _make_validator(VALID_DATABAG)
        conn = ConnStub(cursor_stub=CursorStub(fetchone_row=(1,)))

        with patch("validators.trino_client.validator.trino.dbapi.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        query_check = next(c for c in result.checks if c.name == "query")
        assert query_check.passed
        assert conn.closed

    def test_fails_when_query_returns_unexpected_result(self) -> None:
        # GIVEN a coordinator that returns something other than 1
        validator = _make_validator(VALID_DATABAG)
        conn = ConnStub(cursor_stub=CursorStub(fetchone_row=(0,)))

        with patch("validators.trino_client.validator.trino.dbapi.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        query_check = next(c for c in result.checks if c.name == "query")
        assert not query_check.passed

    def test_fails_when_query_raises(self) -> None:
        # GIVEN a coordinator whose SELECT 1 raises
        validator = _make_validator(VALID_DATABAG)
        conn = ConnStub(cursor_stub=CursorStub(execute_error=RuntimeError("query error")))

        with patch("validators.trino_client.validator.trino.dbapi.connect", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        query_check = next(c for c in result.checks if c.name == "query")
        assert not query_check.passed
        assert "query error" in query_check.message

    def test_fails_connect_check_when_secret_resolution_raises(self) -> None:
        # GIVEN credential resolution that fails
        validator = _make_validator({**VALID_DATABAG, "user-secret-id": "secret:missing"})

        with patch.object(TrinoClientValidator, "_resolve_credentials", side_effect=RuntimeError("secret error")):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "secret error" in connect_check.message

    def test_fails_schema_check_when_discovery_uri_missing(self) -> None:
        # GIVEN a databag missing the required discovery-uri field
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
