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

"""Unit tests for the config-server interface validator.

Coverage targets
----------------
* L1 (simple) — requires role: ≥1 valid + ≥2 invalid scenarios.
* L2 (deep)   — requires role: ≥1 valid + ≥2 invalid scenarios.
* L1 (simple) — provides role: ≥1 valid + ≥2 invalid scenarios.
"""

from typing import Any, cast
from unittest.mock import MagicMock, patch

import ops

from validators.config_server.validator import (
    ConfigServerValidator,
    _check_config_server_db_format,
    _check_extra_user_roles,
    _parse_hosts,
    _parse_replica_set,
)
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_CONFIG_SERVER_DB = "mongodb-k8s/mongodb-k8s-0.mongodb-k8s-endpoints.testing.svc.cluster.local:27017"

_VALID_PROVIDER_DATABAG: dict[str, str] = {
    "config-server-db": _CONFIG_SERVER_DB,
    "key-file": "supersecretkeyfile",
    "username": "mongos-router",
    "password": "routerpassword",
}

_VALID_REQUIRER_DATABAG: dict[str, str] = {
    "database": "mongos-k8s_testing",
    "extra-user-roles": "admin",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_validator(
    databag: dict[str, str],
    endpoint: str = "cluster",
    role: RelationRoleStub = RelationRoleStub.requires,
    interface: str = "config-server",
) -> ConfigServerValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    charm = cast(ops.CharmBase, make_charm_from_relation(relation, interface_name=interface, role=role))
    return ConfigServerValidator(charm, cast(ops.Relation, relation))


def _make_validator_no_remote_app(
    endpoint: str = "cluster",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> ConfigServerValidator:
    """Produce a validator where the remote app is absent (relation.app is None)."""
    relation = RelationStub(name=endpoint, id=0, app=None, data={})
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(relation, interface_name="config-server", role=role),
    )
    return ConfigServerValidator(charm, cast(ops.Relation, relation))


def _mock_mongo_client(ping_raises: Exception | None = None, db_names: list[str] | None = None) -> Any:
    """Return a MagicMock MongoClient suitable for patching _build_mongo_client."""
    client = MagicMock()
    if ping_raises is not None:
        client.admin.command.side_effect = ping_raises
    else:
        client.admin.command.return_value = {"ok": 1.0}
    client.list_database_names.return_value = db_names if db_names is not None else ["admin", "config", "local"]
    return client


# ---------------------------------------------------------------------------
# Pure-helper unit tests (no charm context needed)
# ---------------------------------------------------------------------------


class TestCheckConfigServerDbFormat:
    def test_valid_single_host(self) -> None:
        check = _check_config_server_db_format(
            "mongodb-k8s/mongodb-k8s-0.mongodb-k8s-endpoints.testing.svc.cluster.local:27017"
        )
        assert check.passed, check.message

    def test_valid_multiple_hosts(self) -> None:
        check = _check_config_server_db_format(
            "rs0/host-0.ns.svc.cluster.local:27017,host-1.ns.svc.cluster.local:27017"
        )
        assert check.passed, check.message

    def test_invalid_missing_replicaset_prefix(self) -> None:
        check = _check_config_server_db_format("host-0.ns.svc.cluster.local:27017")
        assert not check.passed
        assert "format" in check.message.lower()

    def test_invalid_empty_string(self) -> None:
        check = _check_config_server_db_format("")
        assert not check.passed
        assert "empty" in check.message.lower()

    def test_invalid_plain_mongodb_uri(self) -> None:
        check = _check_config_server_db_format("mongodb://user:pass@host:27017/admin")
        assert not check.passed


class TestCheckExtraUserRoles:
    def test_valid_single_role(self) -> None:
        check = _check_extra_user_roles("admin")
        assert check.passed

    def test_valid_multiple_roles(self) -> None:
        check = _check_extra_user_roles("admin,readWrite")
        assert check.passed

    def test_invalid_empty_string(self) -> None:
        check = _check_extra_user_roles("")
        assert not check.passed
        assert "empty" in check.message.lower()

    def test_invalid_whitespace_only(self) -> None:
        check = _check_extra_user_roles("   ,  ")
        assert not check.passed


class TestParseHelpers:
    def test_parse_hosts_single(self) -> None:
        hosts = _parse_hosts("rs0/host-0:27017")
        assert hosts == [("host-0", 27017)]

    def test_parse_hosts_multiple(self) -> None:
        hosts = _parse_hosts("rs0/host-0:27017,host-1:27018")
        assert hosts == [("host-0", 27017), ("host-1", 27018)]

    def test_parse_replica_set(self) -> None:
        assert _parse_replica_set("mongodb-k8s/host:27017") == "mongodb-k8s"


# ---------------------------------------------------------------------------
# L1 — requires role (mongos reads provider databag)
# ---------------------------------------------------------------------------


class TestRequiresSimple:
    """L1 validation on the requires (mongos) side."""

    def test_valid_databag_passes(self) -> None:
        """Valid L1: all fields present, TCP skipped with mock."""
        validator = _make_validator(_VALID_PROVIDER_DATABAG)

        with patch("validators.config_server.validator.socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__.return_value = mock_conn.return_value
            mock_conn.return_value.__exit__.return_value = False
            result = validator.validate(level="simple")

        assert result.status == "PASS", result
        assert result.level == "simple"
        assert result.interface == "config-server"
        schema = next(c for c in result.checks if c.name == "schema")
        assert schema.passed
        fmt = next(c for c in result.checks if c.name == "config_server_db_format")
        assert fmt.passed
        tcp = next(c for c in result.checks if c.name == "tcp_connect")
        assert tcp.passed

    def test_invalid_missing_required_fields(self) -> None:
        """Invalid L1: provider published no fields → schema FAIL."""
        validator = _make_validator({})

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "config-server-db" in schema.message
        assert "key-file" in schema.message
        assert "username" in schema.message
        assert "password" in schema.message

    def test_invalid_malformed_config_server_db(self) -> None:
        """Invalid L1: config-server-db has wrong format → format check FAIL."""
        bad_databag = {**_VALID_PROVIDER_DATABAG, "config-server-db": "mongodb://user:pass@host:27017/admin"}
        validator = _make_validator(bad_databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        fmt = next(c for c in result.checks if c.name == "config_server_db_format")
        assert not fmt.passed
        assert "format" in fmt.message.lower()

    def test_invalid_tcp_unreachable(self) -> None:
        """Invalid L1: TCP connection refused → tcp_connect FAIL."""
        validator = _make_validator(_VALID_PROVIDER_DATABAG)

        with patch(
            "validators.config_server.validator.socket.create_connection",
            side_effect=OSError("Connection refused"),
        ):
            result = validator.validate(level="simple")

        assert result.status == "FAIL"
        tcp = next(c for c in result.checks if c.name == "tcp_connect")
        assert not tcp.passed
        assert "Connection refused" in tcp.message

    def test_no_remote_app_returns_error(self) -> None:
        """Invalid L1: remote app absent → ERROR result."""
        validator = _make_validator_no_remote_app()
        result = validator.validate(level="simple")
        assert result.status == "ERROR"
        assert result.error is not None

    def test_skips_unsupported_level(self) -> None:
        """L1: unsupported level (uat) → SKIPPED."""
        validator = _make_validator(_VALID_PROVIDER_DATABAG)
        result = validator.validate(level="uat")
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_provides_and_peer_roles_skipped(self) -> None:
        """L1: peer role is not supported → SKIPPED."""
        validator = _make_validator(_VALID_PROVIDER_DATABAG, role=RelationRoleStub.peer)
        result = validator.validate(level="simple")
        assert result.status == "SKIPPED", f"Expected SKIPPED for peer, got {result.status}"

    def test_credentials_resolved_via_secret_user(self) -> None:
        """L1: username/password stored as a Juju secret (secret-user key)."""
        databag_with_secret: dict[str, str] = {
            "config-server-db": _CONFIG_SERVER_DB,
            "key-file": "keyfile-value",
            "secret-user": "secret:abc123",
        }
        validator = _make_validator(databag_with_secret)

        secret_stub = MagicMock()
        secret_stub.get_content.return_value = {"username": "mongos-router", "password": "routerpassword"}
        validator.charm.model.get_secret = MagicMock(return_value=secret_stub)

        with patch("validators.config_server.validator.socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__.return_value = mock_conn.return_value
            mock_conn.return_value.__exit__.return_value = False
            result = validator.validate(level="simple")

        assert result.status == "PASS", result
        fmt = next(c for c in result.checks if c.name == "config_server_db_format")
        assert fmt.passed

    def test_extra_fields_resolved_via_secret_extra(self) -> None:
        """L1: config-server-db and key-file stored under secret-extra (data_platform_libs pattern)."""
        databag_with_secrets: dict[str, str] = {
            "secret-extra": "secret:extra123",
            "secret-user": "secret:user456",
        }
        validator = _make_validator(databag_with_secrets)

        def _get_secret(id: str) -> MagicMock:  # noqa: A002
            stub = MagicMock()
            if "extra" in id:
                stub.get_content.return_value = {"config-server-db": _CONFIG_SERVER_DB, "key-file": "keyfile-value"}
            else:
                stub.get_content.return_value = {"username": "mongos-router", "password": "routerpassword"}
            return stub

        validator.charm.model.get_secret = _get_secret  # type: ignore[assignment]

        with patch("validators.config_server.validator.socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__.return_value = mock_conn.return_value
            mock_conn.return_value.__exit__.return_value = False
            result = validator.validate(level="simple")

        assert result.status == "PASS", result
        fmt = next(c for c in result.checks if c.name == "config_server_db_format")
        assert fmt.passed


# ---------------------------------------------------------------------------
# L2 — requires role (pymongo deep probe)
# ---------------------------------------------------------------------------


class TestRequiresDeep:
    """L2 validation on the requires (mongos) side."""

    def test_valid_full_deep_pass(self) -> None:
        """Valid L2: ping succeeds, list_databases succeeds → PASS."""
        validator = _make_validator(_VALID_PROVIDER_DATABAG)
        mock_client = _mock_mongo_client(db_names=["admin", "config", "local"])

        with (
            patch("validators.config_server.validator.socket.create_connection") as mock_conn,
            patch("validators.config_server.validator._build_mongo_client", return_value=mock_client),
        ):
            mock_conn.return_value.__enter__.return_value = mock_conn.return_value
            mock_conn.return_value.__exit__.return_value = False
            result = validator.validate(level="deep")

        assert result.status == "PASS", result
        assert result.level == "deep"
        ping = next(c for c in result.checks if c.name == "ping")
        assert ping.passed
        list_db = next(c for c in result.checks if c.name == "list_databases")
        assert list_db.passed
        latency = next(c for c in result.checks if c.name == "latency")
        assert latency.passed

    def test_invalid_ping_fails(self) -> None:
        """Invalid L2: pymongo ping raises → ping check FAIL."""
        from pymongo.errors import ServerSelectionTimeoutError

        validator = _make_validator(_VALID_PROVIDER_DATABAG)
        mock_client = _mock_mongo_client(ping_raises=ServerSelectionTimeoutError("timeout"))

        with (
            patch("validators.config_server.validator.socket.create_connection") as mock_conn,
            patch("validators.config_server.validator._build_mongo_client", return_value=mock_client),
        ):
            mock_conn.return_value.__enter__.return_value = mock_conn.return_value
            mock_conn.return_value.__exit__.return_value = False
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        ping = next(c for c in result.checks if c.name == "ping")
        assert not ping.passed
        assert "timeout" in ping.message.lower() or "remediation" in ping.message.lower()

    def test_invalid_list_databases_fails(self) -> None:
        """Invalid L2: ping succeeds but list_database_names raises → list_databases FAIL."""
        from pymongo.errors import OperationFailure

        validator = _make_validator(_VALID_PROVIDER_DATABAG)
        mock_client = _mock_mongo_client()
        mock_client.list_database_names.side_effect = OperationFailure("not authorised")

        with (
            patch("validators.config_server.validator.socket.create_connection") as mock_conn,
            patch("validators.config_server.validator._build_mongo_client", return_value=mock_client),
        ):
            mock_conn.return_value.__enter__.return_value = mock_conn.return_value
            mock_conn.return_value.__exit__.return_value = False
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        list_db = next(c for c in result.checks if c.name == "list_databases")
        assert not list_db.passed
        assert "authorised" in list_db.message.lower() or "remediation" in list_db.message.lower()

    def test_invalid_missing_required_fields(self) -> None:
        """Invalid L2: empty provider databag → schema FAIL before any connection."""
        validator = _make_validator({})
        result = validator.validate(level="deep")

        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert not any(c.name == "ping" for c in result.checks)

    def test_invalid_tcp_unreachable_stops_before_pymongo(self) -> None:
        """Invalid L2: TCP check fails → stops before attempting pymongo connection."""
        validator = _make_validator(_VALID_PROVIDER_DATABAG)

        with patch(
            "validators.config_server.validator.socket.create_connection",
            side_effect=OSError("Connection refused"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        tcp = next(c for c in result.checks if c.name == "tcp_connect")
        assert not tcp.passed
        assert not any(c.name == "ping" for c in result.checks), "Should not attempt pymongo ping when TCP fails"

    def test_no_remote_app_returns_error(self) -> None:
        """Invalid L2: remote app absent → ERROR result."""
        validator = _make_validator_no_remote_app()
        result = validator.validate(level="deep")
        assert result.status == "ERROR"

    def test_mongo_client_closed_on_exception(self) -> None:
        """L2: MongoClient is always closed, even when ping raises."""
        from pymongo.errors import ServerSelectionTimeoutError

        validator = _make_validator(_VALID_PROVIDER_DATABAG)
        mock_client = _mock_mongo_client(ping_raises=ServerSelectionTimeoutError("timeout"))

        with (
            patch("validators.config_server.validator.socket.create_connection") as mock_conn,
            patch("validators.config_server.validator._build_mongo_client", return_value=mock_client),
        ):
            mock_conn.return_value.__enter__.return_value = mock_conn.return_value
            mock_conn.return_value.__exit__.return_value = False
            validator.validate(level="deep")

        mock_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# L1 — provides role (config-server reads mongos requirer databag)
# ---------------------------------------------------------------------------


class TestProvidesSimple:
    """L1 validation on the provides (config-server) side."""

    def test_valid_requirer_databag_passes(self) -> None:
        """Valid L1: requirer published all expected fields → PASS."""
        validator = _make_validator(_VALID_REQUIRER_DATABAG, role=RelationRoleStub.provides)
        result = validator.validate(level="simple")

        assert result.status == "PASS", result
        schema = next(c for c in result.checks if c.name == "schema")
        assert schema.passed
        roles = next(c for c in result.checks if c.name == "extra_user_roles")
        assert roles.passed

    def test_invalid_database_field_missing(self) -> None:
        """Invalid L1: requirer did not publish 'database' → schema FAIL."""
        validator = _make_validator({"extra-user-roles": "admin"}, role=RelationRoleStub.provides)
        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "database" in schema.message

    def test_invalid_extra_user_roles_missing(self) -> None:
        """Invalid L1: requirer did not publish 'extra-user-roles' → extra_user_roles FAIL."""
        validator = _make_validator({"database": "mongos-k8s_testing"}, role=RelationRoleStub.provides)
        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        roles = next(c for c in result.checks if c.name == "extra_user_roles")
        assert not roles.passed
        assert "empty" in roles.message.lower()

    def test_invalid_extra_user_roles_empty(self) -> None:
        """Invalid L1: extra-user-roles present but empty string → extra_user_roles FAIL."""
        validator = _make_validator(
            {"database": "mongos-k8s_testing", "extra-user-roles": ""},
            role=RelationRoleStub.provides,
        )
        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        roles = next(c for c in result.checks if c.name == "extra_user_roles")
        assert not roles.passed
        assert "empty" in roles.message.lower()

    def test_no_remote_app_returns_error(self) -> None:
        """Invalid L1: remote app absent → ERROR result."""
        validator = _make_validator_no_remote_app(role=RelationRoleStub.provides)
        result = validator.validate(level="simple")
        assert result.status == "ERROR"

    def test_deep_level_skipped_for_provides(self) -> None:
        """L2 not supported for provides role → SKIPPED."""
        validator = _make_validator(_VALID_REQUIRER_DATABAG, role=RelationRoleStub.provides)
        result = validator.validate(level="deep")
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        """Result metadata is correctly populated."""
        validator = _make_validator(_VALID_REQUIRER_DATABAG, role=RelationRoleStub.provides, endpoint="my-cluster")
        result = validator.validate(level="simple")

        assert result.endpoint == "my-cluster"
        assert result.interface == "config-server"
        assert result.role == "provides"
