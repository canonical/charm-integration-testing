# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import cast
from unittest.mock import MagicMock, patch

import ops
import pytest

import validators.parca_store.validator as _parca_store_mod
from validators.parca_store.validator import ParcaStoreValidator, _grpc_channel_ready_check, _parse_grpc_address
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "remote-store-address": "10.1.2.3:7070",
    "remote-store-bearer-token": "test-token",
    "remote-store-insecure": "true",
}


def _make_validator(
    databag: dict[str, str],
    endpoint: str = "external-parca-store-endpoint",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> ParcaStoreValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(relation, interface_name="parca_store", role=role),
    )
    return ParcaStoreValidator(charm, cast(ops.Relation, relation))


# ---------------------------------------------------------------------------
# Helpers: _parse_grpc_address
# ---------------------------------------------------------------------------


class TestParseGrpcAddress:
    def test_parses_standard_host_port(self) -> None:
        check, host, port = _parse_grpc_address("10.1.2.3:7070")
        assert check.passed
        assert host == "10.1.2.3"
        assert port == 7070

    def test_strips_scheme_prefix(self) -> None:
        check, host, port = _parse_grpc_address("grpc://parca.ns.svc:7070")
        assert check.passed
        assert host == "parca.ns.svc"
        assert port == 7070

    def test_strips_ipv6_brackets(self) -> None:
        check, host, port = _parse_grpc_address("[2001:db8::1]:7070")
        assert check.passed
        assert host == "2001:db8::1"
        assert port == 7070

    def test_fails_when_no_port(self) -> None:
        check, host, port = _parse_grpc_address("10.1.2.3")
        assert not check.passed
        assert "no port" in check.message
        assert host == ""
        assert port == 0

    def test_fails_on_invalid_port(self) -> None:
        check, host, port = _parse_grpc_address("10.1.2.3:notaport")
        assert not check.passed
        assert port == 0


# ---------------------------------------------------------------------------
# L1 — simple
# ---------------------------------------------------------------------------


class TestParcaStoreValidatorSimple:
    def test_skips_for_unsupported_level(self) -> None:
        v = _make_validator(VALID_DATABAG)
        result = v.validate(level="uat")
        assert result.status == "SKIPPED"
        assert result.error is not None

    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.requires, False),
            (RelationRoleStub.provides, True),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_skips_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        v = _make_validator(VALID_DATABAG, role=role)
        result = v.validate(level="simple")
        assert (result.status == "SKIPPED") == should_skip

    def test_error_when_no_remote_app(self) -> None:
        relation = RelationStub(name="external-parca-store-endpoint", id=0, app=None, data={})
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="parca_store"),
        )
        v = ParcaStoreValidator(charm, cast(ops.Relation, relation))
        result = v.validate(level="simple")
        assert result.status == "ERROR"

    def test_fails_schema_when_address_missing(self) -> None:
        v = _make_validator({})
        result = v.validate(level="simple")
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "remote-store-address" in schema_check.message

    def test_fails_parse_when_address_malformed(self) -> None:
        v = _make_validator({"remote-store-address": "no-port-here", "remote-store-insecure": "true"})
        result = v.validate(level="simple")
        assert result.status == "FAIL"
        parse_check = next(c for c in result.checks if c.name == "parse")
        assert not parse_check.passed

    def test_fails_connect_when_host_unreachable(self) -> None:
        v = _make_validator(VALID_DATABAG)
        with patch("validators.parca_store.validator.socket.create_connection", side_effect=OSError("refused")):
            result = v.validate(level="simple")
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "refused" in connect_check.message

    def test_passes_simple_when_tcp_succeeds_insecure(self) -> None:
        v = _make_validator(VALID_DATABAG)  # insecure=true, no TLS check
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("validators.parca_store.validator.socket.create_connection", return_value=mock_conn):
            result = v.validate(level="simple")
        assert result.status == "PASS"
        assert any(c.name == "connect" and c.passed for c in result.checks)
        # No TLS check because insecure=true
        assert not any(c.name == "tls" for c in result.checks)

    def test_includes_tls_check_when_not_insecure(self) -> None:
        from validators.base import ValidationCheck

        databag = {**VALID_DATABAG, "remote-store-insecure": "false"}
        v = _make_validator(databag)
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        tls_check = ValidationCheck(name="tls", passed=True, message="OK")
        with (
            patch("validators.parca_store.validator.socket.create_connection", return_value=mock_conn),
            patch("validators.parca_store.validator._tls_prerequisite_check", return_value=tls_check) as mock_tls,
        ):
            result = v.validate(level="simple")
        mock_tls.assert_called_once()
        assert result.status == "PASS"

    def test_sets_endpoint_and_interface(self) -> None:
        v = _make_validator(VALID_DATABAG, endpoint="my-grpc-ep")
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("validators.parca_store.validator.socket.create_connection", return_value=mock_conn):
            result = v.validate(level="simple")
        assert result.endpoint == "my-grpc-ep"
        assert result.interface == "parca_store"


# ---------------------------------------------------------------------------
# L2 — deep
# ---------------------------------------------------------------------------


class TestParcaStoreValidatorDeep:
    def test_skips_for_uat_level(self) -> None:
        v = _make_validator(VALID_DATABAG)
        result = v.validate(level="uat")
        assert result.status == "SKIPPED"

    def test_error_when_no_remote_app(self) -> None:
        relation = RelationStub(name="external-parca-store-endpoint", id=0, app=None, data={})
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="parca_store"),
        )
        v = ParcaStoreValidator(charm, cast(ops.Relation, relation))
        result = v.validate(level="deep")
        assert result.status == "ERROR"

    def test_fails_schema_when_address_missing(self) -> None:
        v = _make_validator({})
        result = v.validate(level="deep")
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed

    def test_fails_parse_when_address_malformed(self) -> None:
        v = _make_validator({"remote-store-address": "bad-address", "remote-store-insecure": "true"})
        result = v.validate(level="deep")
        assert result.status == "FAIL"
        parse_check = next(c for c in result.checks if c.name == "parse")
        assert not parse_check.passed

    def test_fails_tcp_when_host_unreachable(self) -> None:
        v = _make_validator(VALID_DATABAG)
        with patch("validators.parca_store.validator.socket.create_connection", side_effect=OSError("refused")):
            result = v.validate(level="deep")
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        # Should not attempt gRPC if TCP fails
        assert not any(c.name == "grpc_ready" for c in result.checks)

    def test_fails_grpc_ready_when_channel_timeout(self) -> None:
        from validators.base import ValidationCheck

        v = _make_validator(VALID_DATABAG)
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        grpc_fail = ValidationCheck(name="grpc_ready", passed=False, message="timeout")
        with (
            patch("validators.parca_store.validator.socket.create_connection", return_value=mock_conn),
            patch("validators.parca_store.validator._grpc_channel_ready_check", return_value=grpc_fail) as mock_grpc,
        ):
            result = v.validate(level="deep")
        assert result.status == "FAIL"
        mock_grpc.assert_called_once()
        grpc_check = next(c for c in result.checks if c.name == "grpc_ready")
        assert not grpc_check.passed

    def test_passes_deep_when_grpc_ready(self) -> None:
        from validators.base import ValidationCheck

        v = _make_validator(VALID_DATABAG)
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        grpc_ok = ValidationCheck(name="grpc_ready", passed=True, message="READY")
        with (
            patch("validators.parca_store.validator.socket.create_connection", return_value=mock_conn),
            patch("validators.parca_store.validator._grpc_channel_ready_check", return_value=grpc_ok) as mock_grpc,
        ):
            result = v.validate(level="deep")
        assert result.status == "PASS"
        mock_grpc.assert_called_once()
        grpc_check = next(c for c in result.checks if c.name == "grpc_ready")
        assert grpc_check.passed


# ---------------------------------------------------------------------------
# _grpc_channel_ready_check — channel selection logic
# ---------------------------------------------------------------------------

# Patch the `grpc` attribute on the validator module directly so these tests can
# unit-test channel selection logic with a lightweight mock implementation.


class _FakeTimeoutError(Exception):
    """Stand-in for grpc.FutureTimeoutError in channel-selection tests."""


def _make_mock_grpc() -> MagicMock:
    mock = MagicMock()
    mock.FutureTimeoutError = _FakeTimeoutError
    return mock


class TestGrpcChannelReadyCheck:
    def test_uses_insecure_channel_when_insecure_true(self) -> None:
        mock_grpc = _make_mock_grpc()
        mock_channel = MagicMock()
        mock_future = MagicMock()
        mock_future.result.return_value = None
        mock_grpc.insecure_channel.return_value = mock_channel
        mock_grpc.channel_ready_future.return_value = mock_future

        with patch.object(_parca_store_mod, "grpc", mock_grpc):
            check = _grpc_channel_ready_check("10.0.0.1:7070", insecure=True, token="")

        mock_grpc.insecure_channel.assert_called_once_with("10.0.0.1:7070")
        mock_grpc.secure_channel.assert_not_called()
        assert check.passed

    def test_uses_secure_channel_without_token(self) -> None:
        mock_grpc = _make_mock_grpc()
        mock_channel = MagicMock()
        mock_future = MagicMock()
        mock_future.result.return_value = None
        mock_ssl_creds = MagicMock()
        mock_grpc.ssl_channel_credentials.return_value = mock_ssl_creds
        mock_grpc.secure_channel.return_value = mock_channel
        mock_grpc.channel_ready_future.return_value = mock_future

        with patch.object(_parca_store_mod, "grpc", mock_grpc):
            check = _grpc_channel_ready_check("10.0.0.1:7070", insecure=False, token="")

        mock_grpc.insecure_channel.assert_not_called()
        mock_grpc.access_token_call_credentials.assert_not_called()
        mock_grpc.secure_channel.assert_called_once_with("10.0.0.1:7070", mock_ssl_creds)
        assert check.passed

    def test_uses_composite_credentials_with_token(self) -> None:
        mock_grpc = _make_mock_grpc()
        mock_channel = MagicMock()
        mock_future = MagicMock()
        mock_future.result.return_value = None
        mock_ssl_creds = MagicMock()
        mock_call_creds = MagicMock()
        mock_composite_creds = MagicMock()
        mock_grpc.ssl_channel_credentials.return_value = mock_ssl_creds
        mock_grpc.access_token_call_credentials.return_value = mock_call_creds
        mock_grpc.composite_channel_credentials.return_value = mock_composite_creds
        mock_grpc.secure_channel.return_value = mock_channel
        mock_grpc.channel_ready_future.return_value = mock_future

        with patch.object(_parca_store_mod, "grpc", mock_grpc):
            check = _grpc_channel_ready_check("10.0.0.1:7070", insecure=False, token="my-token")

        mock_grpc.access_token_call_credentials.assert_called_once_with("my-token")
        mock_grpc.composite_channel_credentials.assert_called_once_with(mock_ssl_creds, mock_call_creds)
        mock_grpc.secure_channel.assert_called_once_with("10.0.0.1:7070", mock_composite_creds)
        assert check.passed

    def test_returns_failed_check_on_timeout(self) -> None:
        mock_grpc = _make_mock_grpc()
        mock_channel = MagicMock()
        mock_future = MagicMock()
        mock_future.result.side_effect = _FakeTimeoutError()
        mock_grpc.insecure_channel.return_value = mock_channel
        mock_grpc.channel_ready_future.return_value = mock_future

        with patch.object(_parca_store_mod, "grpc", mock_grpc):
            check = _grpc_channel_ready_check("10.0.0.1:7070", insecure=True, token="")

        assert not check.passed
        assert "10.0.0.1:7070" in check.message

    def test_returns_failed_check_on_generic_exception(self) -> None:
        mock_grpc = _make_mock_grpc()
        mock_grpc.insecure_channel.side_effect = RuntimeError("boom")

        with patch.object(_parca_store_mod, "grpc", mock_grpc):
            check = _grpc_channel_ready_check("10.0.0.1:7070", insecure=True, token="")

        assert not check.passed
        assert "boom" in check.message
