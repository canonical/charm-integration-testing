# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import MagicMock, patch

import grpc
import ops
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from validators.etcd_client.validator import (
    ETCD_CLIENT_CERT_PATH_ENV,
    ETCD_CLIENT_KEY_PATH_ENV,
    EtcdClientValidator,
    _decode_message,
    _encode_bytes_field,
)
from validators.test_utils.helpers import make_charm_from_relation, make_charm_from_relation_and_secrets
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)


def _make_validator(
    databag: dict[str, str],
    endpoint: str = "etcd-client",
    role: RelationRoleStub = RelationRoleStub.requires,
    local_databag: dict[str, str] | None = None,
) -> EtcdClientValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    stub_charm = make_charm_from_relation(relation, interface_name="etcd_client", role=role)
    if local_databag is not None:
        relation.data[stub_charm.app] = local_databag
    return EtcdClientValidator(cast(ops.CharmBase, stub_charm), cast(ops.Relation, relation))


def _make_fake_kv_channel(stored: dict[str, bytes], get_value: Any) -> MagicMock:
    """Build a fake grpc.Channel whose unary_unary() mimics etcd's KV service.

    Encodes/decodes using the validator module's own hand-rolled protobuf wire
    helpers, so the fake responses are realistic without needing generated stubs.
    """

    def unary_unary(method: str, request_serializer: Any = None, response_deserializer: Any = None) -> Any:
        def call(request: bytes, timeout: float = 0) -> bytes:
            if method.endswith("/Put"):
                fields = _decode_message(request)
                stored["key"] = fields.get(1, [b""])[0]  # type: ignore[assignment]
                stored["value"] = fields.get(2, [b""])[0]  # type: ignore[assignment]
                return b""
            if method.endswith("/Range"):
                request_fields = _decode_message(request)
                requested_key = request_fields.get(1, [b""])[0]
                value = get_value() if callable(get_value) else get_value
                key_value_msg = _encode_bytes_field(1, requested_key) + _encode_bytes_field(  # type: ignore[arg-type]
                    5, value
                )
                return _encode_bytes_field(2, key_value_msg)
            if method.endswith("/DeleteRange"):
                return b""
            raise AssertionError(f"unexpected method {method}")

        return call

    channel = MagicMock()
    channel.unary_unary.side_effect = unary_unary
    channel.__enter__.return_value = channel
    channel.__exit__.return_value = False
    return channel


def _generate_cert(common_name: str, not_after_days: int = 365) -> tuple[str, str]:
    """Generate a self-signed certificate and matching private key PEM for tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=not_after_days))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


VALID_CA_PEM, _VALID_CA_KEY_PEM = _generate_cert("ca_common_name")
VALID_CLIENT_CERT_PEM, VALID_CLIENT_KEY_PEM = _generate_cert("client-user")
EXPIRED_CLIENT_CERT_PEM, _ = _generate_cert("expired-user", not_after_days=-1)

VALID_REQUIRER_DATABAG: dict[str, str] = {
    "endpoints": "10.1.2.3:2379,10.1.2.4:2379",
    "uris": "https://10.1.2.3:2379,https://10.1.2.4:2379",
    "username": "client-user",
    "tls-ca": VALID_CA_PEM,
    "tls": "enabled",
    "version": "3.6.0",
}

VALID_PROVIDER_DATABAG: dict[str, str] = {
    "prefix": "/my-app/",
    "mtls-cert": VALID_CLIENT_CERT_PEM,
}

# The requirer's own contribution to the relation (its own local app databag),
# used by requires-side deep-level tests that exercise identity matching.
VALID_LOCAL_REQUIRER_DATABAG: dict[str, str] = {
    "prefix": "myprefix-",
    "mtls-cert": VALID_CLIENT_CERT_PEM,
}


class TestEtcdClientValidatorRole:
    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.requires, False),
            (RelationRoleStub.provides, False),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_skips_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # An empty databag is enough here: this test only asserts the role-based
        # skip/dispatch behavior, not full validation, and a populated requires-side
        # databag would otherwise make a real (slow, network-dependent) TCP connect
        # attempt via _check_tcp_reachable.
        validator = _make_validator({}, role=role)

        result = validator.validate(level="simple")

        assert (result.status == "SKIPPED") == should_skip

    def test_returns_skipped_for_unsupported_level(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        result = validator.validate(level="uat")

        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_returns_error_when_no_remote_app(self) -> None:
        app = ApplicationStub()
        relation = RelationStub(name="etcd-client", id=0, app=app, data={})
        del relation.data[app]
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="etcd_client", role=RelationRoleStub.requires),
        )
        validator = EtcdClientValidator(charm, cast(ops.Relation, relation))

        result = validator.validate(level="simple")

        assert result.status == "ERROR"


class TestEtcdClientValidatorRequiresSimple:
    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        validator = _make_validator({})

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        for field in ("endpoints", "uris", "username", "tls-ca", "version"):
            assert field in schema_check.message

    @pytest.mark.parametrize(
        "bad_value,description",
        [
            ("10.1.2.3", "missing port"),
            ("10.1.2.3:notaport", "non-numeric port"),
            ("10.1.2.3:0", "port zero"),
            ("10.1.2.3:99999", "port out of range"),
            ("10.1.2.3:\u00b2", "digit-like but non-numeric port"),
            ("[::1:2379", "unbalanced ipv6 brackets"),
            ("::1:2379", "unbracketed ipv6 host"),
            ("[10.1.2.3:2379", "one-sided bracket around non-colon host"),
            ("[10.1.2.3]:2379", "balanced brackets around a non-ipv6 host"),
            ("[]:2379", "balanced brackets around an empty host"),
            ("10.1.2.3:1_000", "port with underscore separator"),
            ("10.1.2.3:+2379", "port with explicit sign"),
        ],
    )
    def test_fails_endpoints_format_check(self, bad_value: str, description: str) -> None:
        databag = {**VALID_REQUIRER_DATABAG, "endpoints": bad_value}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL", f"Expected FAIL for {description}"
        check = next(c for c in result.checks if c.name == "endpoints_format")
        assert not check.passed

    def test_redacts_userinfo_from_invalid_endpoints_message(self) -> None:
        # A malformed "endpoints" entry carrying userinfo must not leak the credential into
        # the endpoints_format failure message.
        secret = "hunter2"
        bad_endpoint = "admin:" + secret + "@10.1.2.3:notaport"
        databag = {**VALID_REQUIRER_DATABAG, "endpoints": bad_endpoint}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "endpoints_format")
        assert not check.passed
        assert secret not in check.message

    def test_redacts_userinfo_containing_an_embedded_at_sign(self) -> None:
        # A userinfo segment containing a literal embedded "@" (e.g. "admin@secret@host")
        # must be fully redacted, not just up to the first "@".
        secret = "hunter2"
        bad_endpoint = "admin@" + secret + "@10.1.2.3:notaport"
        databag = {**VALID_REQUIRER_DATABAG, "endpoints": bad_endpoint}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "endpoints_format")
        assert not check.passed
        assert secret not in check.message

    def test_fails_endpoints_format_check_for_overlong_digit_port(self) -> None:
        # A port string with far more digits than any valid port (max 65535, 5 digits) must
        # fail cleanly as a normal FAIL, not crash validation into ERROR: int() itself raises
        # ValueError for excessively long all-digit strings (Python's integer-string
        # conversion limit), so the length must be bounded before ever calling int().
        bad_endpoint = "10.1.2.3:" + "9" * 5000
        databag = {**VALID_REQUIRER_DATABAG, "endpoints": bad_endpoint}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "endpoints_format")
        assert not check.passed

    def test_rejects_userinfo_in_otherwise_valid_endpoint(self) -> None:
        # Even with a well-formed host:port, an endpoint carrying userinfo (including a
        # percent-encoded credential, which would not contain a literal ":" for the naive
        # rpartition(":")-based port split to stumble over) must still be rejected: this
        # interface authenticates via mTLS and a separate "username" field, never a
        # "user:pass@" URI prefix.
        secret = "hunter2"
        bad_endpoint = "admin%3A" + secret + "@10.1.2.3:2379"
        databag = {**VALID_REQUIRER_DATABAG, "endpoints": bad_endpoint}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "endpoints_format")
        assert not check.passed

    def test_fails_uris_format_at_simple_level_when_malformed(self) -> None:
        databag = {**VALID_REQUIRER_DATABAG, "uris": "https://10.1.2.3:notaport"}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "uris_format")
        assert not check.passed

    def test_fails_tls_ca_pem_check_when_invalid(self) -> None:
        databag = {**VALID_REQUIRER_DATABAG, "tls-ca": "not-a-pem"}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "tls_ca_pem")
        assert not check.passed

    @pytest.mark.parametrize("disabled_value", ["disabled", "false", "False"])
    def test_fails_tls_enabled_check_when_tls_not_advertised(self, disabled_value: str) -> None:
        databag = {**VALID_REQUIRER_DATABAG, "tls": disabled_value}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "tls_enabled")
        assert not check.passed

    def test_fails_uris_format_check_for_whitespace_in_hostname(self) -> None:
        databag = {**VALID_REQUIRER_DATABAG, "uris": "https://bad host:2379"}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "uris_format")
        assert not check.passed

    def test_passes_with_all_required_fields_and_reachable_endpoint(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        with patch("validators.etcd_client.validator.socket.create_connection") as mock_connect:
            mock_connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            result = validator.validate(level="simple")

        assert result.status == "PASS"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert connect_check.passed

    def test_fails_connect_check_when_unreachable(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        with patch("validators.etcd_client.validator.socket.create_connection", side_effect=OSError("refused")):
            result = validator.validate(level="simple")

        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed

    def test_strips_brackets_from_ipv6_endpoint_before_connecting(self) -> None:
        databag = {**VALID_REQUIRER_DATABAG, "endpoints": "[::1]:2379"}
        validator = _make_validator(databag)

        with patch("validators.etcd_client.validator.socket.create_connection") as mock_connect:
            mock_connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            result = validator.validate(level="simple")

        assert result.status == "PASS", result.checks
        mock_connect.assert_called_once_with(("::1", 2379), timeout=3.0)

    def test_connects_to_first_non_empty_endpoint_entry(self) -> None:
        # A leading empty comma-separated segment (e.g. ",10.1.2.3:2379") is itself filtered
        # out by _check_endpoints_format's own non-empty-entries list, so it passes format
        # validation; _check_tcp_reachable must derive "first" from that same filtered list
        # rather than a naive split(",")[0], or it would try to connect to an empty host.
        databag = {**VALID_REQUIRER_DATABAG, "endpoints": ",10.1.2.3:2379"}
        validator = _make_validator(databag)

        with patch("validators.etcd_client.validator.socket.create_connection") as mock_connect:
            mock_connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            result = validator.validate(level="simple")

        assert result.status == "PASS", result.checks
        mock_connect.assert_called_once_with(("10.1.2.3", 2379), timeout=3.0)


class TestEtcdClientValidatorRequiresDeep:
    def test_fails_when_client_identity_not_provisioned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN no cert/key material provisioned at the documented paths
        monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, "/nonexistent/client.pem")
        monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, "/nonexistent/client.key")
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        result = validator.validate(level="deep")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "client_identity")
        assert not check.passed
        assert "never conveys a private key" in check.message

    def test_fails_client_identity_read_check_when_file_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN cert/key files that pass the isfile() existence check but fail to
        # open (e.g. a permissions change race): this must be reported under a
        # client-identity-specific check, not misattributed to "put" as if a PUT had
        # been attempted.
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(VALID_CLIENT_CERT_PEM)
            with open(key_path, "w") as f:
                f.write(VALID_CLIENT_KEY_PEM)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            validator = _make_validator(VALID_REQUIRER_DATABAG, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

            with patch("builtins.open", side_effect=OSError("permission denied")):
                result = validator.validate(level="deep")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "client_identity_read")
        assert not check.passed
        assert not any(c.name == "put" for c in result.checks)

    def test_fails_when_local_prefix_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a local databag missing "prefix" entirely (as opposed to an
        # intentionally empty string): writing a canary without it would target an
        # unscoped key rather than reporting the malformed local databag.
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(VALID_CLIENT_CERT_PEM)
            with open(key_path, "w") as f:
                f.write(VALID_CLIENT_KEY_PEM)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            local_databag = {"mtls-cert": VALID_CLIENT_CERT_PEM}  # no "prefix"
            validator = _make_validator(VALID_REQUIRER_DATABAG, local_databag=local_databag)

            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "prefix_present")
        assert not check.passed
        for name in ("put", "get", "delete"):
            assert not any(c.name == name for c in result.checks)

    def test_fails_client_identity_check_when_credentials_are_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a malformed private key that grpc.ssl_channel_credentials rejects:
        # this must be reported as a failed check instead of an unhandled exception.
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(VALID_CLIENT_CERT_PEM)
            with open(key_path, "w") as f:
                f.write("not-a-valid-key")
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            validator = _make_validator(VALID_REQUIRER_DATABAG, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

            with patch(
                "validators.etcd_client.validator.grpc.ssl_channel_credentials",
                side_effect=ValueError("invalid private key"),
            ):
                result = validator.validate(level="deep")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "client_credentials")
        assert not check.passed
        for name in ("put", "get", "delete"):
            assert not any(c.name == name for c in result.checks)

    def test_fails_schema_before_reaching_identity_check(self) -> None:
        validator = _make_validator({})

        result = validator.validate(level="deep")

        assert result.status == "FAIL"
        assert not any(c.name == "client_identity" for c in result.checks)

    def test_passes_full_read_write_cycle_with_provisioned_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(VALID_CLIENT_CERT_PEM)
            with open(key_path, "w") as f:
                f.write(VALID_CLIENT_KEY_PEM)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            validator = _make_validator(VALID_REQUIRER_DATABAG, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

            stored: dict[str, bytes] = {}
            fake_channel = _make_fake_kv_channel(stored, get_value=lambda: stored.get("value", b""))

            with (
                patch("validators.etcd_client.validator.grpc.ssl_channel_credentials"),
                patch("validators.etcd_client.validator.grpc.secure_channel", return_value=fake_channel),
            ):
                result = validator.validate(level="deep")

        assert result.status == "PASS", result.checks
        for name in ("put", "get", "delete"):
            check = next(c for c in result.checks if c.name == name)
            assert check.passed
        # Regression guard: the canary key written must be scoped under the requirer's own
        # local "prefix" (VALID_LOCAL_REQUIRER_DATABAG), not e.g. a regressed read of the
        # provider's remote databag, which has no "prefix" field of its own.
        assert stored["key"].decode().startswith(VALID_LOCAL_REQUIRER_DATABAG["prefix"])

    def test_fails_get_check_when_value_mismatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(VALID_CLIENT_CERT_PEM)
            with open(key_path, "w") as f:
                f.write(VALID_CLIENT_KEY_PEM)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            validator = _make_validator(VALID_REQUIRER_DATABAG, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

            fake_channel = _make_fake_kv_channel({}, get_value=lambda: b"wrong-value")

            with (
                patch("validators.etcd_client.validator.grpc.ssl_channel_credentials"),
                patch("validators.etcd_client.validator.grpc.secure_channel", return_value=fake_channel),
            ):
                result = validator.validate(level="deep")

        assert result.status == "FAIL"
        get_check = next(c for c in result.checks if c.name == "get")
        assert not get_check.passed

    def test_fails_get_check_when_returned_key_mismatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a Range response whose KeyValue.key does not match the requested canary
        # key (e.g. etcd returned an unrelated key that happens to hold the expected
        # value): the GET check must fail rather than accept the value alone.
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(VALID_CLIENT_CERT_PEM)
            with open(key_path, "w") as f:
                f.write(VALID_CLIENT_KEY_PEM)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            validator = _make_validator(VALID_REQUIRER_DATABAG, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

            stored: dict[str, bytes] = {}

            def unary_unary(method: str, request_serializer: Any = None, response_deserializer: Any = None) -> Any:
                def call(request: bytes, timeout: float = 0) -> bytes:
                    if method.endswith("/Put"):
                        fields = _decode_message(request)
                        stored["value"] = fields.get(2, [b""])[0]  # type: ignore[assignment]
                        return b""
                    if method.endswith("/Range"):
                        key_value_msg = _encode_bytes_field(1, b"some-other-key") + _encode_bytes_field(
                            5, stored.get("value", b"")
                        )
                        return _encode_bytes_field(2, key_value_msg)
                    if method.endswith("/DeleteRange"):
                        return b""
                    raise AssertionError(f"unexpected method {method}")

                return call

            fake_channel = MagicMock()
            fake_channel.unary_unary.side_effect = unary_unary
            fake_channel.__enter__.return_value = fake_channel
            fake_channel.__exit__.return_value = False

            with (
                patch("validators.etcd_client.validator.grpc.ssl_channel_credentials"),
                patch("validators.etcd_client.validator.grpc.secure_channel", return_value=fake_channel),
            ):
                result = validator.validate(level="deep")

        assert result.status == "FAIL"
        get_check = next(c for c in result.checks if c.name == "get")
        assert not get_check.passed
        assert "key mismatch" in get_check.message.lower()

    def test_attempts_cleanup_when_put_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a PUT that fails at the transport level (e.g. a client-side timeout
        # that may still have committed server-side): DeleteRange cleanup must still
        # be attempted so no canary key is orphaned, and GET must be skipped since
        # PUT did not confirmably succeed.
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(VALID_CLIENT_CERT_PEM)
            with open(key_path, "w") as f:
                f.write(VALID_CLIENT_KEY_PEM)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            validator = _make_validator(VALID_REQUIRER_DATABAG, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

            delete_calls: list[bytes] = []

            class _FakeRpcError(grpc.RpcError):
                def details(self) -> str:
                    return "deadline exceeded"

            def unary_unary(method: str, request_serializer: Any = None, response_deserializer: Any = None) -> Any:
                def call(request: bytes, timeout: float = 0) -> bytes:
                    if method.endswith("/Put"):
                        raise _FakeRpcError()
                    if method.endswith("/DeleteRange"):
                        delete_calls.append(request)
                        return b""
                    raise AssertionError(f"unexpected method {method}")

                return call

            fake_channel = MagicMock()
            fake_channel.unary_unary.side_effect = unary_unary
            fake_channel.__enter__.return_value = fake_channel
            fake_channel.__exit__.return_value = False

            with (
                patch("validators.etcd_client.validator.grpc.ssl_channel_credentials"),
                patch("validators.etcd_client.validator.grpc.secure_channel", return_value=fake_channel),
            ):
                result = validator.validate(level="deep")

        assert result.status == "FAIL"
        put_check = next(c for c in result.checks if c.name == "put")
        assert not put_check.passed
        assert not any(c.name == "get" for c in result.checks)
        delete_check = next(c for c in result.checks if c.name == "delete")
        assert delete_check.passed
        assert len(delete_calls) == 1

    def test_fails_delete_check_when_delete_rpc_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a successful PUT/GET but a DeleteRange call that fails at the transport
        # level: the "delete" check must fail, and validate() must complete gracefully
        # (not raise) rather than propagate the RpcError.
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(VALID_CLIENT_CERT_PEM)
            with open(key_path, "w") as f:
                f.write(VALID_CLIENT_KEY_PEM)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            validator = _make_validator(VALID_REQUIRER_DATABAG, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

            class _FakeRpcError(grpc.RpcError):
                def details(self) -> str:
                    return "deadline exceeded"

            stored: dict[str, bytes] = {}

            def unary_unary(method: str, request_serializer: Any = None, response_deserializer: Any = None) -> Any:
                def call(request: bytes, timeout: float = 0) -> bytes:
                    if method.endswith("/Put"):
                        fields = _decode_message(request)
                        stored["key"] = fields.get(1, [b""])[0]  # type: ignore[assignment]
                        stored["value"] = fields.get(2, [b""])[0]  # type: ignore[assignment]
                        return b""
                    if method.endswith("/Range"):
                        key_value_msg = _encode_bytes_field(1, stored["key"]) + _encode_bytes_field(5, stored["value"])
                        return _encode_bytes_field(2, key_value_msg)
                    if method.endswith("/DeleteRange"):
                        raise _FakeRpcError()
                    raise AssertionError(f"unexpected method {method}")

                return call

            fake_channel = MagicMock()
            fake_channel.unary_unary.side_effect = unary_unary
            fake_channel.__enter__.return_value = fake_channel
            fake_channel.__exit__.return_value = False

            with (
                patch("validators.etcd_client.validator.grpc.ssl_channel_credentials"),
                patch("validators.etcd_client.validator.grpc.secure_channel", return_value=fake_channel),
            ):
                result = validator.validate(level="deep")

        assert result.status == "FAIL"
        put_check = next(c for c in result.checks if c.name == "put")
        assert put_check.passed
        get_check = next(c for c in result.checks if c.name == "get")
        assert get_check.passed
        delete_check = next(c for c in result.checks if c.name == "delete")
        assert not delete_check.passed
        assert "deadline exceeded" in delete_check.message

    def test_fails_identity_match_when_local_cert_does_not_match_published_cert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GIVEN a locally-provisioned identity that is valid but is NOT the cert
        # actually published on this relation (e.g. env vars pointing at some
        # other identity's cert/key by mistake).
        other_cert_pem, other_key_pem = _generate_cert("someone-elses-identity")
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(other_cert_pem)
            with open(key_path, "w") as f:
                f.write(other_key_pem)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            validator = _make_validator(VALID_REQUIRER_DATABAG, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        identity_check = next(c for c in result.checks if c.name == "identity_match")
        assert not identity_check.passed
        for name in ("put", "get", "delete"):
            assert not any(c.name == name for c in result.checks)

    def test_fails_username_matches_cert_cn_check_when_username_does_not_match_cert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GIVEN a locally-provisioned client cert that IS the one published on this
        # relation (identity_match passes), but the provider's "username" field claims
        # a different identity than the cert's own common name.
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(VALID_CLIENT_CERT_PEM)
            with open(key_path, "w") as f:
                f.write(VALID_CLIENT_KEY_PEM)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            databag = {**VALID_REQUIRER_DATABAG, "username": "someone-else"}
            validator = _make_validator(databag, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        username_check = next(c for c in result.checks if c.name == "username_matches_cert_cn")
        assert not username_check.passed
        for name in ("put", "get", "delete"):
            assert not any(c.name == name for c in result.checks)

    def test_fails_identity_match_gracefully_when_local_cert_file_is_binary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GIVEN a cert file containing non-UTF-8 bytes: this must be reported as a
        # failed identity_match check rather than crashing validate() with an
        # unhandled UnicodeDecodeError.
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "wb") as f:
                f.write(b"\xff\xfe\x00not-utf8-and-not-pem")
            with open(key_path, "w") as f:
                f.write(VALID_CLIENT_KEY_PEM)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            validator = _make_validator(VALID_REQUIRER_DATABAG, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        identity_check = next(c for c in result.checks if c.name == "identity_match")
        assert not identity_check.passed

    def test_passes_full_read_write_cycle_with_secret_backed_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN provider fields (username/uris/tls-ca) AND the requirer's own
        # mtls-cert resolved via secrets rather than plaintext, matching library
        # revisions that publish both as secret groups. This exercises the deep
        # path end-to-end so both _resolve_requirer_side_credentials() and
        # _resolve_local_mtls_cert()'s secret-backed branches are actually run.
        databag = {
            "endpoints": VALID_REQUIRER_DATABAG["endpoints"],
            "version": VALID_REQUIRER_DATABAG["version"],
            "secret-user": "secret:etcd-user",
            "secret-tls": "secret:etcd-tls",
        }
        local_databag = {"prefix": VALID_LOCAL_REQUIRER_DATABAG["prefix"], "secret-mtls": "secret:local-mtls"}
        secrets = {
            "secret:etcd-user": {
                "username": VALID_REQUIRER_DATABAG["username"],
                "uris": VALID_REQUIRER_DATABAG["uris"],
            },
            "secret:etcd-tls": {"tls-ca": VALID_REQUIRER_DATABAG["tls-ca"], "tls": VALID_REQUIRER_DATABAG["tls"]},
            "secret:local-mtls": {"mtls-cert": VALID_CLIENT_CERT_PEM},
        }
        app = ApplicationStub()
        relation = RelationStub(name="etcd-client", id=0, app=app, data={app: databag})
        charm = make_charm_from_relation_and_secrets(relation, secrets, role=RelationRoleStub.requires)
        relation.data[charm.app] = local_databag
        validator = EtcdClientValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(VALID_CLIENT_CERT_PEM)
            with open(key_path, "w") as f:
                f.write(VALID_CLIENT_KEY_PEM)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            stored: dict[str, bytes] = {}
            fake_channel = _make_fake_kv_channel(stored, get_value=lambda: stored.get("value", b""))

            with (
                patch("validators.etcd_client.validator.grpc.ssl_channel_credentials"),
                patch("validators.etcd_client.validator.grpc.secure_channel", return_value=fake_channel),
            ):
                result = validator.validate(level="deep")

        assert result.status == "PASS", result.checks
        assert "secret:etcd-user" in charm.model.requested_ids
        assert "secret:etcd-tls" in charm.model.requested_ids
        assert "secret:local-mtls" in charm.model.requested_ids
        # Regression guard: same as test_passes_full_read_write_cycle_with_provisioned_identity,
        # confirms the canary key is scoped under the requirer's own local "prefix".
        assert stored["key"].decode().startswith(VALID_LOCAL_REQUIRER_DATABAG["prefix"])


class TestEtcdClientValidatorGrpcTarget:
    @pytest.mark.parametrize(
        "uris,expected_target",
        [
            ("https://10.1.2.3:2379", "10.1.2.3:2379"),
            ("10.1.2.3:2379", "10.1.2.3:2379"),
            ("https://[::1]:2379", "[::1]:2379"),
            ("[::1]:2379", "[::1]:2379"),
        ],
    )
    def test_formats_ipv4_and_ipv6_targets(self, uris: str, expected_target: str) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        target, check = validator._pick_grpc_target(uris)

        assert check.passed
        assert target == expected_target

    def test_fails_uris_format_check_for_out_of_range_port(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        target, check = validator._pick_grpc_target("10.1.2.3:99999")

        assert not check.passed
        assert target == ""

    def test_rejects_userinfo_in_uri(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        uri = "https://" + "admin" + ":" + "hunter2" + "@10.1.2.3:2379"

        target, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert target == ""
        assert "userinfo" in check.message.lower()

    def test_rejects_uri_with_empty_userinfo(self) -> None:
        # GIVEN a uri with an empty (but present) username/password, e.g. "https://@host:2379":
        # urlsplit() reports username=="" (falsy) rather than None, so a truthiness check alone
        # would let this slip through. It must still be rejected as userinfo.
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        target, check = validator._pick_grpc_target("https://@10.1.2.3:2379")

        assert not check.passed
        assert target == ""
        assert "userinfo" in check.message.lower()

    def test_rejects_uri_with_path_component(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        target, check = validator._pick_grpc_target("https://10.1.2.3:2379/not-etcd")

        assert not check.passed
        assert target == ""

    def test_redacts_userinfo_from_unparseable_uri_message(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        secret = "hunter2"
        uri = "https://admin:" + secret + "@"

        # A URI with userinfo but no valid host/port still must not leak the
        # credential into the failure message.
        target, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert target == ""
        assert secret not in check.message
        assert "<redacted>" in check.message

    def test_redacts_userinfo_and_query_from_malformed_port_message(self) -> None:
        # A malformed uri whose port fails to parse (triggering urlsplit's own ValueError)
        # must still not leak userinfo or a query-string secret into the check message.
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        secret = "hunter2"
        uri = "https://admin:" + secret + "@10.1.2.3:notaport?token=" + secret

        target, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert target == ""
        assert secret not in check.message
        assert "token" not in check.message

    def test_redacts_userinfo_from_scheme_less_uri_message(self) -> None:
        # A bare "host:port"-style uris entry (no "//" scheme separator) can still carry
        # userinfo; the redaction must apply regardless of scheme presence.
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        secret = "hunter2"
        uri = "admin:" + secret + "@10.1.2.3:2379"

        target, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert target == ""
        assert secret not in check.message

    def test_rejects_unsupported_uri_scheme(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        target, check = validator._pick_grpc_target("http://10.1.2.3:2379")

        assert not check.passed
        assert target == ""

    def test_fails_when_second_entry_is_malformed(self) -> None:
        # GIVEN a "uris" field with a valid first entry but a malformed second entry: the
        # malformed entry must not be silently ignored just because the first one is fine.
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        target, check = validator._pick_grpc_target("https://10.1.2.3:2379,https://10.1.2.4:notaport")

        assert not check.passed
        assert target == ""

    def test_uses_first_entry_target_when_all_entries_are_valid(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        target, check = validator._pick_grpc_target("https://10.1.2.3:2379,https://10.1.2.4:2379")

        assert check.passed
        assert target == "10.1.2.3:2379"


class TestEtcdClientValidatorProvidesSimple:
    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        validator = _make_validator({}, role=RelationRoleStub.provides)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "prefix" in schema_check.message
        assert "mtls-cert" in schema_check.message

    def test_fails_when_mtls_cert_not_parseable(self) -> None:
        databag = {**VALID_PROVIDER_DATABAG, "mtls-cert": "not-a-pem"}
        validator = _make_validator(databag, role=RelationRoleStub.provides)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "mtls_cert_parseable")
        assert not check.passed

    def test_fails_when_mtls_cert_expired(self) -> None:
        databag = {**VALID_PROVIDER_DATABAG, "mtls-cert": EXPIRED_CLIENT_CERT_PEM}
        validator = _make_validator(databag, role=RelationRoleStub.provides)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "validity_period")
        assert not check.passed

    def test_passes_with_valid_cert(self) -> None:
        validator = _make_validator(VALID_PROVIDER_DATABAG, role=RelationRoleStub.provides)

        result = validator.validate(level="simple")

        assert result.status == "PASS"
        for name in ("schema", "mtls_cert_parseable", "validity_period"):
            check = next(c for c in result.checks if c.name == name)
            assert check.passed

    def test_passes_with_secret_backed_mtls_cert(self) -> None:
        # GIVEN mtls-cert resolved via a secret group rather than a plaintext field,
        # matching data-integrator revisions observed publishing it that way.
        databag = {"prefix": VALID_PROVIDER_DATABAG["prefix"], "secret-mtls": "secret:etcd-mtls"}
        secrets = {"secret:etcd-mtls": {"mtls-cert": VALID_PROVIDER_DATABAG["mtls-cert"]}}
        app = ApplicationStub()
        relation = RelationStub(name="etcd-client", id=0, app=app, data={app: databag})
        charm = make_charm_from_relation_and_secrets(relation, secrets, role=RelationRoleStub.provides)
        validator = EtcdClientValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        result = validator.validate(level="simple")

        assert result.status == "PASS", result.checks
        assert charm.model.requested_ids == ["secret:etcd-mtls"]


class TestEtcdClientValidatorProvidesDeep:
    def test_returns_skipped_for_deep_level(self) -> None:
        validator = _make_validator(VALID_PROVIDER_DATABAG, role=RelationRoleStub.provides)

        result = validator.validate(level="deep")

        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_returns_fail_for_deep_level_when_l1_check_fails(self) -> None:
        # GIVEN an expired cert: an L1 failure must not be discarded by the deep-level
        # SKIP, since that would let an invalid relation pass via the runner's
        # simple-level fallback.
        databag = {**VALID_PROVIDER_DATABAG, "mtls-cert": EXPIRED_CLIENT_CERT_PEM}
        validator = _make_validator(databag, role=RelationRoleStub.provides)

        result = validator.validate(level="deep")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "validity_period")
        assert not check.passed

    def test_returns_error_when_no_remote_app_before_skip(self) -> None:
        app = ApplicationStub()
        relation = RelationStub(name="etcd-client", id=0, app=app, data={})
        del relation.data[app]
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="etcd_client", role=RelationRoleStub.provides),
        )
        validator = EtcdClientValidator(charm, cast(ops.Relation, relation))

        result = validator.validate(level="deep")

        assert result.status == "ERROR"
