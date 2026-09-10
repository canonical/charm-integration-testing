# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import MagicMock, call, patch

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
    _encode_varint_field,
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


def _make_fake_kv_channel(stored: dict[str, bytes], get_value: Any, put_keys: list[bytes] | None = None) -> MagicMock:
    """Build a fake grpc.Channel whose unary_unary() mimics etcd's KV service.

    Encodes/decodes using the validator module's own hand-rolled protobuf wire
    helpers, so the fake responses are realistic without needing generated stubs.

    `stored` models etcd's actual current state (mutated by PUT/DELETE), while the optional
    `put_keys` list independently records every key ever PUT, in order, so tests can still
    assert on the canary key used even after a passing DELETE has removed it from `stored`.
    """

    def unary_unary(method: str, request_serializer: Any = None, response_deserializer: Any = None) -> Any:
        def call(request: bytes, timeout: float = 0) -> bytes:
            if method.endswith("/Put"):
                fields = _decode_message(request)
                stored["key"] = fields.get(1, [b""])[0]  # type: ignore[assignment]
                stored["value"] = fields.get(2, [b""])[0]  # type: ignore[assignment]
                if put_keys is not None:
                    put_keys.append(stored["key"])
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
                request_fields = _decode_message(request)
                requested_key = request_fields.get(1, [b""])[0]
                # Model deletion for real (rather than unconditionally succeeding without
                # effect): only remove the stored key/value if the request's key actually
                # matches what was stored, and leave `stored` untouched otherwise, so tests
                # can assert the canary is genuinely gone after a passing validation (and
                # would catch a regression that deletes the wrong key or leaves the canary
                # behind). The response's "deleted" count (field 2) reflects this too, since
                # the validator itself now checks that count rather than trusting a bare
                # successful gRPC status.
                deleted_count = 1 if stored.get("key") == requested_key else 0
                if deleted_count:
                    stored.pop("key", None)
                    stored.pop("value", None)
                return _encode_varint_field(2, deleted_count)
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
EXPIRED_CA_PEM, _ = _generate_cert("expired-ca_common_name", not_after_days=-1)

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


class TestProtobufWireHelpers:
    """Independently verifies the hand-rolled protobuf wire helpers against literal,
    by-hand-computed byte fixtures (not derived by round-tripping through the same
    encoder/decoder pair), so a shared bug in both encode and decode can't hide behind
    a self-consistent fake gRPC channel in the rest of this test file.
    """

    def test_encodes_bytes_field_matching_hand_computed_wire_bytes(self) -> None:
        # GIVEN field number 1 (tag byte (1 << 3) | 2 = 0x0A) and the 3-byte value b"foo"
        # WHEN encoding it as a length-delimited field
        result = _encode_bytes_field(1, b"foo")
        # THEN the bytes are exactly: tag 0x0A, length 0x03, then the raw value.
        assert result == b"\x0a\x03foo"

    def test_encodes_varint_field_matching_protobuf_documentation_example(self) -> None:
        # GIVEN field number 2 (tag byte (2 << 3) | 0 = 0x10) and the value 150, whose
        # varint encoding (0x96, 0x01) is the canonical worked example from Google's
        # protobuf wire-format documentation (there, for field 1: "08 96 01").
        result = _encode_varint_field(2, 150)
        # THEN the bytes are exactly the tag followed by that well-known varint encoding.
        assert result == b"\x10\x96\x01"

    def test_decodes_hand_written_bytes_for_single_length_delimited_field(self) -> None:
        # GIVEN a hand-written message (not produced by this module's own encoder)
        # containing one length-delimited field, number 1, value b"foo"
        raw = b"\x0a\x03foo"
        # WHEN decoding it
        fields = _decode_message(raw)
        # THEN it recovers exactly that field/value.
        assert fields == {1: [b"foo"]}

    def test_decodes_hand_written_bytes_for_single_varint_field(self) -> None:
        # GIVEN the same canonical protobuf documentation example, hand-written directly
        raw = b"\x10\x96\x01"
        # WHEN decoding it
        fields = _decode_message(raw)
        # THEN it recovers field number 2 with the integer value 150.
        assert fields == {2: [150]}

    def test_decodes_hand_written_bytes_for_mixed_field_message(self) -> None:
        # GIVEN a hand-written message combining a length-delimited field (number 1,
        # value b"key") and a varint field (number 2, value 5), interleaved as etcd's
        # own KeyValue messages are (key bytes followed by a numeric field)
        raw = b"\x0a\x03key" + b"\x10\x05"
        # WHEN decoding it
        fields = _decode_message(raw)
        # THEN both fields are recovered independently of each other.
        assert fields == {1: [b"key"], 2: [5]}


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

    def test_redacts_userinfo_with_query_delimiter_before_terminating_at_sign(self) -> None:
        # GIVEN a malformed uri where a "?" appears within the userinfo segment itself, before
        # its own terminating "@" (e.g. "admin:secret?token@host"): the userinfo-redaction
        # regex must not stop at the "?" and let the query-stripping regex run first, which
        # would otherwise strip only "?token@host..." and leave "admin:secret" exposed in the
        # message.
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        secret = "hunter2"
        uri = "https://admin:" + secret + "?token@host:2379"

        targets, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert targets == []
        assert secret not in check.message
        assert "<redacted>" in check.message

    def test_redacts_userinfo_containing_slash_before_terminating_at_sign(self) -> None:
        # GIVEN a malformed uri where a "?" and a "/" both appear within the userinfo segment
        # itself, before its own terminating "@" (e.g. "admin:secret?token/foo@host"): the
        # userinfo redaction must not stop at either delimiter, since a naive redaction that
        # only crosses "?"/"#" but still stops at the first "/" would leave "admin:secret"
        # exposed (the "://" scheme separator is handled separately, by locating it via
        # substring search rather than by scanning past every "/" in the rest of the string).
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        secret = "hunter2"
        uri = "https://admin:" + secret + "?token/foo@host:2379"

        targets, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert targets == []
        assert secret not in check.message
        assert "<redacted>" in check.message
        assert check.message.startswith("Could not parse uri 'https://<redacted>@host:2379'")

    def test_redacts_userinfo_in_scheme_less_uri_containing_bare_double_slash(self) -> None:
        # GIVEN a malformed, scheme-less uri (no "://" at all) whose userinfo segment happens
        # to contain a bare "//" before its own terminating "@" (e.g.
        # "admin:secret@host//path:2379"): redaction must not mistake that "//" for a scheme
        # separator and treat everything before it as an un-redactable prefix, since this
        # entry never had a scheme in the first place. Only a genuine "://" delimits a scheme.
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        secret = "hunter2"
        uri = "admin:" + secret + "@host//path:2379"

        targets, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert targets == []
        assert secret not in check.message
        assert "<redacted>" in check.message

    def test_redacts_userinfo_when_uri_contains_a_scheme_separator_past_the_start(self) -> None:
        # GIVEN a malformed uri whose userinfo segment itself embeds a "://" (e.g.
        # "admin:secret@https://host:2379" or "admin:secret://token@host:2379"): a naive
        # redaction that treats the *first* "://" anywhere in the string as a trusted scheme
        # delimiter would leave everything before it -- including the credential -- outside
        # the redacted remainder. Only a scheme matching at the *start* of the string counts.
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        secret = "hunter2"
        uri = "admin:" + secret + "@https://host:2379"

        targets, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert targets == []
        assert secret not in check.message
        assert "<redacted>" in check.message

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

    def test_fails_tls_ca_validity_period_check_when_ca_expired(self) -> None:
        # An expired CA is a distinct failure mode from an expired client cert (checked
        # elsewhere on the provides side): it must fail cleanly at L1, before the deep
        # probe attempts to build TLS credentials from it.
        databag = {**VALID_REQUIRER_DATABAG, "tls-ca": EXPIRED_CA_PEM}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "tls_ca_validity_period")
        assert not check.passed

    @pytest.mark.parametrize("disabled_value", ["disabled", "false", "False"])
    def test_fails_tls_enabled_check_when_tls_not_advertised(self, disabled_value: str) -> None:
        databag = {**VALID_REQUIRER_DATABAG, "tls": disabled_value}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "tls_enabled")
        assert not check.passed

    @pytest.mark.parametrize("typo_value", ["flase", "no", "0", "enable"])
    def test_fails_tls_enabled_check_for_unrecognized_value(self, typo_value: str) -> None:
        # An allowlist (not a denylist) must be used for "tls": an unrecognized spelling
        # like a typo of "enabled"/"true" must be treated as not-enabled rather than
        # silently passing just because it doesn't match one of the known-disabled strings.
        databag = {**VALID_REQUIRER_DATABAG, "tls": typo_value}
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

    def test_fails_uris_format_check_for_control_character_in_hostname(self) -> None:
        # urlsplit() is lenient about C0 control characters (e.g. an embedded NUL byte)
        # inside a hostname, unlike whitespace's separate check above; a gRPC target built
        # from such a hostname would just fail to connect, so it must be rejected as a
        # format error instead.
        databag = {**VALID_REQUIRER_DATABAG, "uris": "https://127.0.0.1\x00:2379"}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "uris_format")
        assert not check.passed

    def test_fails_endpoints_format_check_for_whitespace_in_hostname(self) -> None:
        # Mirrors test_fails_uris_format_check_for_whitespace_in_hostname: "endpoints" must
        # reject whitespace in the host the same way "uris" does, rather than accepting an
        # invalid host that would only fail later (and more confusingly) at connect time.
        databag = {**VALID_REQUIRER_DATABAG, "endpoints": "bad host:2379"}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "endpoints_format")
        assert not check.passed

    def test_fails_endpoints_format_check_for_control_character_in_hostname(self) -> None:
        databag = {**VALID_REQUIRER_DATABAG, "endpoints": "127.0.0.1\x00:2379"}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "endpoints_format")
        assert not check.passed

    def test_fails_endpoints_format_check_for_uri_delimiter_in_second_entry(self) -> None:
        # GIVEN a reachable first endpoint but a second entry with a URI delimiter embedded
        # in its host (e.g. "host/path"): a naive rpartition(":")-based split doesn't itself
        # reject this the way urlsplit()-based _parse_single_uri does for "uris", so it must
        # be checked explicitly here too, regardless of which field _check_tcp_reachable()
        # itself now derives its connect targets from ("endpoints" must still be
        # schema/format-valid on its own merits).
        databag = {**VALID_REQUIRER_DATABAG, "endpoints": "10.1.2.3:2379,host/path:2379"}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "endpoints_format")
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

    def test_strips_brackets_from_ipv6_target_before_connecting(self) -> None:
        # GIVEN a "uris" entry advertising a bracketed IPv6 literal: the connect targets for
        # simple-level validation are derived from the same already-parsed "uris" targets
        # deep validation uses (not from the separate "endpoints" field), so this must be
        # driven by "uris" here.
        databag = {**VALID_REQUIRER_DATABAG, "uris": "https://[::1]:2379"}
        validator = _make_validator(databag)

        with patch("validators.etcd_client.validator.socket.create_connection") as mock_connect:
            mock_connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            result = validator.validate(level="simple")

        assert result.status == "PASS", result.checks
        mock_connect.assert_called_once_with(("::1", 2379), timeout=3.0)

    def test_connects_to_first_advertised_uris_target(self) -> None:
        # GIVEN "uris" advertises two cluster members: simple-level reachability connects to
        # the first one, matching the order "uris" itself advertises them in.
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        with patch("validators.etcd_client.validator.socket.create_connection") as mock_connect:
            mock_connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            result = validator.validate(level="simple")

        assert result.status == "PASS", result.checks
        mock_connect.assert_called_once_with(("10.1.2.3", 2379), timeout=3.0)

    def test_connects_to_second_target_when_first_is_unreachable_at_simple_level(self) -> None:
        # GIVEN "uris" advertises two cluster members and the first is unreachable: simple
        # validation must fail over to the second, matching the same multi-target retry
        # policy L2's read/write probe uses (see
        # test_retries_next_target_when_put_fails_on_first_target), rather than failing
        # simple-level validation just because the first advertised member happens to be
        # down while another is healthy.
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        def fake_create_connection(address: Any, timeout: float = 0) -> MagicMock:
            if address == ("10.1.2.3", 2379):
                raise OSError("refused")
            connection = MagicMock()
            connection.__enter__ = MagicMock(return_value=MagicMock())
            connection.__exit__ = MagicMock(return_value=False)
            return connection

        with patch(
            "validators.etcd_client.validator.socket.create_connection", side_effect=fake_create_connection
        ) as mock_connect:
            result = validator.validate(level="simple")

        assert result.status == "PASS", result.checks
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert connect_check.passed
        assert mock_connect.call_args_list == [
            call(("10.1.2.3", 2379), timeout=3.0),
            call(("10.1.2.4", 2379), timeout=3.0),
        ]

    def test_latency_check_ignores_time_spent_on_a_prior_unreachable_target(self) -> None:
        # GIVEN "uris" advertises two cluster members, the first of which is unreachable and
        # would (in reality) consume most of its connect timeout before failing over: the
        # "latency" check must be based only on the elapsed time of the target that actually
        # produced the returned "connect" check (the second, successful one here), not the
        # cumulative time spent across every attempt. Otherwise a single slow-to-fail member
        # would fail simple validation's latency budget even though the multi-target failover
        # policy is explicitly meant to tolerate exactly this scenario.
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        def fake_create_connection(address: Any, timeout: float = 0) -> MagicMock:
            if address == ("10.1.2.3", 2379):
                raise OSError("refused")
            connection = MagicMock()
            connection.__enter__ = MagicMock(return_value=MagicMock())
            connection.__exit__ = MagicMock(return_value=False)
            return connection

        # time.monotonic() is called: once as the loop's default pre-assignment, once at the
        # start of each target attempt, and once more when a successful attempt returns. The
        # first (failed) target is simulated as having taken 10s -- far beyond the 0.5s simple
        # latency budget -- while the second (successful) target only takes 0.05s.
        with (
            patch("validators.etcd_client.validator.socket.create_connection", side_effect=fake_create_connection),
            patch(
                "validators.etcd_client.validator.time.monotonic",
                side_effect=[0.0, 0.0, 10.0, 10.05],
            ),
        ):
            result = validator.validate(level="simple")

        connect_check = next(c for c in result.checks if c.name == "connect")
        assert connect_check.passed
        latency_check = next(c for c in result.checks if c.name == "latency")
        assert latency_check.passed, latency_check.message
        assert result.status == "PASS", result.checks


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
            put_keys: list[bytes] = []
            fake_channel = _make_fake_kv_channel(stored, get_value=lambda: stored.get("value", b""), put_keys=put_keys)

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
        assert put_keys and put_keys[-1].decode().startswith(VALID_LOCAL_REQUIRER_DATABAG["prefix"])
        # Regression guard: the fake DeleteRange handler above only removes `stored` when the
        # delete request's key actually matches, so this also confirms the DELETE really did
        # target the same canary key that was PUT, not merely that the "delete" check passed.
        assert "key" not in stored

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
                        return _encode_varint_field(2, 1)
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

            # Single uris entry: this test is about cleanup-on-PUT-failure within one target
            # attempt, not the separate multi-target retry behavior (covered by
            # test_retries_next_target_when_put_fails_on_first_target below).
            databag = {**VALID_REQUIRER_DATABAG, "uris": "https://10.1.2.3:2379"}
            validator = _make_validator(databag, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

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
                        # Model the write having actually landed server-side despite the
                        # client-side PUT failure, per this test's own premise (see docstring).
                        return _encode_varint_field(2, 1)
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

    def test_retries_next_target_when_put_fails_on_first_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN "uris" advertises two cluster members and the first is unreachable: the
        # canary must still succeed against the second rather than failing outright, since
        # a single unavailable member shouldn't fail validation of an otherwise-healthy
        # relation.
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
                    return "unavailable"

            targets_seen: list[str] = []
            stored: dict[str, bytes] = {}

            def make_channel_for_target(target: str, credentials: Any = None) -> MagicMock:
                targets_seen.append(target)
                if target == "10.1.2.3:2379":

                    def unary_unary(
                        method: str, request_serializer: Any = None, response_deserializer: Any = None
                    ) -> Any:
                        def call(request: bytes, timeout: float = 0) -> bytes:
                            if method.endswith("/Put"):
                                raise _FakeRpcError()
                            if method.endswith("/DeleteRange"):
                                # Cleanup is attempted for every target tried, even one whose
                                # PUT failed, in case the write partially landed server-side;
                                # model that here so this test's focus stays on the retry
                                # behavior itself, not on cleanup-response fidelity (covered by
                                # test_retries_cleanup_on_next_target_when_put_succeeds_but_delete_fails).
                                return _encode_varint_field(2, 1)
                            raise AssertionError(f"unexpected method {method} on unreachable target")

                        return call

                    channel = MagicMock()
                    channel.unary_unary.side_effect = unary_unary
                    channel.__enter__.return_value = channel
                    channel.__exit__.return_value = False
                    return channel
                return _make_fake_kv_channel(stored, get_value=lambda: stored.get("value", b""))

            with (
                patch("validators.etcd_client.validator.grpc.ssl_channel_credentials"),
                patch(
                    "validators.etcd_client.validator.grpc.secure_channel",
                    side_effect=make_channel_for_target,
                ),
            ):
                result = validator.validate(level="deep")

        assert result.status == "PASS", result.checks
        assert targets_seen == ["10.1.2.3:2379", "10.1.2.4:2379"]
        for name in ("put", "get", "delete"):
            check = next(c for c in result.checks if c.name == name)
            assert check.passed

    def test_retries_cleanup_on_next_target_when_put_succeeds_but_delete_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GIVEN the first target's PUT (and GET verification) succeed, but its own DELETE
        # fails, and a second advertised target is available: the retry loop must not stop
        # simply because PUT succeeded on the first target. It must keep trying the
        # remaining targets so the orphaned canary actually gets cleaned up, only stopping
        # once cleanup has genuinely succeeded (not merely once some target's PUT has).
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

            targets_seen: list[str] = []
            stored: dict[str, bytes] = {}

            def make_channel_for_target(target: str, credentials: Any = None) -> MagicMock:
                targets_seen.append(target)
                if target == "10.1.2.3:2379":

                    def unary_unary(
                        method: str, request_serializer: Any = None, response_deserializer: Any = None
                    ) -> Any:
                        def call(request: bytes, timeout: float = 0) -> bytes:
                            if method.endswith("/Put"):
                                fields = _decode_message(request)
                                stored["key"] = fields.get(1, [b""])[0]  # type: ignore[assignment]
                                stored["value"] = fields.get(2, [b""])[0]  # type: ignore[assignment]
                                return b""
                            if method.endswith("/Range"):
                                requested_key = _decode_message(request).get(1, [b""])[0]
                                value = stored.get("value", b"")
                                key_value_msg = _encode_bytes_field(1, requested_key) + _encode_bytes_field(  # type: ignore[arg-type]
                                    5, value
                                )
                                return _encode_bytes_field(2, key_value_msg)
                            if method.endswith("/DeleteRange"):
                                # Cleanup fails on this target even though the PUT succeeded.
                                raise _FakeRpcError()
                            raise AssertionError(f"unexpected method {method}")

                        return call

                    channel = MagicMock()
                    channel.unary_unary.side_effect = unary_unary
                    channel.__enter__.return_value = channel
                    channel.__exit__.return_value = False
                    return channel
                return _make_fake_kv_channel(stored, get_value=lambda: stored.get("value", b""))

            with (
                patch("validators.etcd_client.validator.grpc.ssl_channel_credentials"),
                patch(
                    "validators.etcd_client.validator.grpc.secure_channel",
                    side_effect=make_channel_for_target,
                ),
            ):
                result = validator.validate(level="deep")

        # The first target's PUT succeeded but its DELETE failed, so the retry loop must
        # have carried on to the second target rather than stopping right after that PUT.
        assert targets_seen == ["10.1.2.3:2379", "10.1.2.4:2379"]
        delete_checks = [c for c in result.checks if c.name == "delete"]
        assert any(not c.passed for c in delete_checks)
        assert any(c.passed for c in delete_checks)
        # The second target's own full cycle (PUT/GET/DELETE) succeeded, and its DELETE
        # really did remove the canary (see _make_fake_kv_channel's DeleteRange handling),
        # so the canary is not left behind even though target 1's cleanup failed.
        assert "key" not in stored

    def test_preserves_cleanup_failure_from_superseded_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN the first target's PUT fails and its own best-effort cleanup DELETE also
        # fails (so a canary key may be orphaned there), and a second target then completes
        # the full PUT/GET/DELETE cycle successfully: the overall result must still surface
        # the first target's cleanup failure rather than reporting an unqualified PASS, since
        # an orphaned canary key is a real (if minor) side effect that shouldn't be hidden
        # just because failover to another target ultimately worked.
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
                    return "unavailable"

            stored: dict[str, bytes] = {}

            def make_channel_for_target(target: str, credentials: Any = None) -> MagicMock:
                if target == "10.1.2.3:2379":

                    def unary_unary(
                        method: str, request_serializer: Any = None, response_deserializer: Any = None
                    ) -> Any:
                        def call(request: bytes, timeout: float = 0) -> bytes:
                            # Both PUT and cleanup DELETE fail on this target.
                            raise _FakeRpcError()

                        return call

                    channel = MagicMock()
                    channel.unary_unary.side_effect = unary_unary
                    channel.__enter__.return_value = channel
                    channel.__exit__.return_value = False
                    return channel
                return _make_fake_kv_channel(stored, get_value=lambda: stored.get("value", b""))

            with (
                patch("validators.etcd_client.validator.grpc.ssl_channel_credentials"),
                patch(
                    "validators.etcd_client.validator.grpc.secure_channel",
                    side_effect=make_channel_for_target,
                ),
            ):
                result = validator.validate(level="deep")

        assert result.status == "FAIL", result.checks
        delete_checks = [c for c in result.checks if c.name == "delete"]
        assert any(not c.passed for c in delete_checks)
        assert any(c.passed for c in delete_checks)
        put_checks = [c for c in result.checks if c.name == "put"]
        assert any(c.passed for c in put_checks)

    def test_fails_get_check_when_response_is_malformed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a successful PUT but a Range RPC that returns truncated/unparseable protobuf
        # bytes: this must be reported as a failed "get" check (and cleanup still attempted),
        # not let the decode error (IndexError/ValueError/TypeError/UnicodeDecodeError)
        # propagate and crash validation into ERROR.
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_path = os.path.join(tmp_dir, "client.pem")
            key_path = os.path.join(tmp_dir, "client.key")
            with open(cert_path, "w") as f:
                f.write(VALID_CLIENT_CERT_PEM)
            with open(key_path, "w") as f:
                f.write(VALID_CLIENT_KEY_PEM)
            monkeypatch.setenv(ETCD_CLIENT_CERT_PATH_ENV, cert_path)
            monkeypatch.setenv(ETCD_CLIENT_KEY_PATH_ENV, key_path)

            databag = {**VALID_REQUIRER_DATABAG, "uris": "https://10.1.2.3:2379"}
            validator = _make_validator(databag, local_databag=VALID_LOCAL_REQUIRER_DATABAG)

            def unary_unary(method: str, request_serializer: Any = None, response_deserializer: Any = None) -> Any:
                def call(request: bytes, timeout: float = 0) -> bytes:
                    if method.endswith("/Put"):
                        return b""
                    if method.endswith("/Range"):
                        # A single 0x80 byte is a varint continuation byte with no following
                        # byte to complete it, so _decode_varint's next read raises IndexError.
                        return b"\x80"
                    if method.endswith("/DeleteRange"):
                        return _encode_varint_field(2, 1)
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
        assert "malformed" in get_check.message.lower()
        delete_check = next(c for c in result.checks if c.name == "delete")
        assert delete_check.passed

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

    def test_fails_delete_check_when_response_reports_zero_deletions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a successful PUT/GET but a DeleteRange call that returns a successful gRPC
        # status while its DeleteRangeResponse.deleted count is 0 (e.g. the request targeted
        # the wrong key, or the key was already gone): the "delete" check must fail rather
        # than trusting the bare absence of an RpcError, since a no-op deletion would
        # otherwise let validation report an unqualified PASS while the canary is left behind.
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
                        stored["key"] = fields.get(1, [b""])[0]  # type: ignore[assignment]
                        stored["value"] = fields.get(2, [b""])[0]  # type: ignore[assignment]
                        return b""
                    if method.endswith("/Range"):
                        key_value_msg = _encode_bytes_field(1, stored["key"]) + _encode_bytes_field(5, stored["value"])
                        return _encode_bytes_field(2, key_value_msg)
                    if method.endswith("/DeleteRange"):
                        # Successful gRPC status, but nothing was actually deleted.
                        return _encode_varint_field(2, 0)
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

    def test_fails_delete_check_when_deleted_count_has_wrong_wire_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN a malformed DeleteRangeResponse where field 2 ("deleted") is encoded as a
        # non-empty length-delimited (bytes) value instead of the varint (int) the real
        # DeleteRangeResponse.deleted field always is. A bare truthiness check on the decoded
        # value would treat this non-empty bytes value as a passed cleanup; the "delete"
        # check must instead require an actual positive int.
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
                        stored["key"] = fields.get(1, [b""])[0]  # type: ignore[assignment]
                        stored["value"] = fields.get(2, [b""])[0]  # type: ignore[assignment]
                        return b""
                    if method.endswith("/Range"):
                        key_value_msg = _encode_bytes_field(1, stored["key"]) + _encode_bytes_field(5, stored["value"])
                        return _encode_bytes_field(2, key_value_msg)
                    if method.endswith("/DeleteRange"):
                        # Field 2 encoded with wire type 2 (length-delimited), not the
                        # varint wire type DeleteRangeResponse.deleted actually uses.
                        return _encode_bytes_field(2, b"\x01")
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
        delete_check = next(c for c in result.checks if c.name == "delete")
        assert not delete_check.passed

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
            put_keys: list[bytes] = []
            fake_channel = _make_fake_kv_channel(stored, get_value=lambda: stored.get("value", b""), put_keys=put_keys)

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
        assert put_keys and put_keys[-1].decode().startswith(VALID_LOCAL_REQUIRER_DATABAG["prefix"])
        # Regression guard: confirms the fake DeleteRange handler's key actually matched and
        # removed the canary, not merely that the "delete" check passed.
        assert "key" not in stored


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

        targets, check = validator._pick_grpc_target(uris)

        assert check.passed
        assert targets == [expected_target]

    def test_fails_uris_format_check_for_out_of_range_port(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        targets, check = validator._pick_grpc_target("10.1.2.3:99999")

        assert not check.passed
        assert targets == []

    def test_rejects_userinfo_in_uri(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        uri = "https://" + "admin" + ":" + "hunter2" + "@10.1.2.3:2379"

        targets, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert targets == []
        assert "userinfo" in check.message.lower()

    def test_rejects_uri_with_empty_userinfo(self) -> None:
        # GIVEN a uri with an empty (but present) username/password, e.g. "https://@host:2379":
        # urlsplit() reports username=="" (falsy) rather than None, so a truthiness check alone
        # would let this slip through. It must still be rejected as userinfo.
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        targets, check = validator._pick_grpc_target("https://@10.1.2.3:2379")

        assert not check.passed
        assert targets == []
        assert "userinfo" in check.message.lower()

    def test_rejects_uri_with_path_component(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        targets, check = validator._pick_grpc_target("https://10.1.2.3:2379/not-etcd")

        assert not check.passed
        assert targets == []

    def test_redacts_userinfo_from_unparseable_uri_message(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        secret = "hunter2"
        uri = "https://admin:" + secret + "@"

        # A URI with userinfo but no valid host/port still must not leak the
        # credential into the failure message.
        targets, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert targets == []
        assert secret not in check.message
        assert "<redacted>" in check.message

    def test_redacts_userinfo_and_query_from_malformed_port_message(self) -> None:
        # A malformed uri whose port fails to parse (triggering urlsplit's own ValueError)
        # must still not leak userinfo or a query-string secret into the check message.
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        secret = "hunter2"
        uri = "https://admin:" + secret + "@10.1.2.3:notaport?token=" + secret

        targets, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert targets == []
        assert secret not in check.message
        assert "token" not in check.message

    def test_redacts_userinfo_from_scheme_less_uri_message(self) -> None:
        # A bare "host:port"-style uris entry (no "//" scheme separator) can still carry
        # userinfo; the redaction must apply regardless of scheme presence.
        validator = _make_validator(VALID_REQUIRER_DATABAG)
        secret = "hunter2"
        uri = "admin:" + secret + "@10.1.2.3:2379"

        targets, check = validator._pick_grpc_target(uri)

        assert not check.passed
        assert targets == []
        assert secret not in check.message

    def test_rejects_unsupported_uri_scheme(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        targets, check = validator._pick_grpc_target("http://10.1.2.3:2379")

        assert not check.passed
        assert targets == []

    def test_fails_when_second_entry_is_malformed(self) -> None:
        # GIVEN a "uris" field with a valid first entry but a malformed second entry: the
        # malformed entry must not be silently ignored just because the first one is fine.
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        targets, check = validator._pick_grpc_target("https://10.1.2.3:2379,https://10.1.2.4:notaport")

        assert not check.passed
        assert targets == []

    def test_returns_all_valid_targets_in_order(self) -> None:
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        targets, check = validator._pick_grpc_target("https://10.1.2.3:2379,https://10.1.2.4:2379")

        assert check.passed
        assert targets == ["10.1.2.3:2379", "10.1.2.4:2379"]

    def test_rejects_network_path_reference_uri(self) -> None:
        # A "//host:port" network-path reference (no scheme) is neither of this interface's
        # two documented forms; urlsplit() would otherwise parse it with an empty scheme,
        # which the scheme allowlist also accepts for bare host:port entries, so it must be
        # rejected explicitly before that ambiguity lets it through.
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        targets, check = validator._pick_grpc_target("//10.1.2.3:2379")

        assert not check.passed
        assert targets == []

    def test_rejects_bracketed_non_ipv6_uri_host(self) -> None:
        # Brackets are only valid syntax around an IPv6 literal (e.g. "[::1]:2379"). On this
        # interpreter, urlsplit() itself already rejects a bracketed non-IPv6 host like
        # "[10.1.2.3]" with a ValueError ("An IPv4 address cannot be in brackets"), which
        # _parse_single_uri's existing except-ValueError branch converts into a failed check;
        # this test locks that behavior in as a regression guard.
        validator = _make_validator(VALID_REQUIRER_DATABAG)

        targets, check = validator._pick_grpc_target("https://[10.1.2.3]:2379")

        assert not check.passed
        assert targets == []


class TestEtcdClientValidatorProvidesSimple:
    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        validator = _make_validator({}, role=RelationRoleStub.provides)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "mtls-cert" in schema_check.message

    def test_fails_prefix_present_check_when_prefix_key_absent(self) -> None:
        # "prefix" must be checked for key presence, not truthiness: an empty prefix (root
        # of the keyspace) is a valid value and must not be rejected the same way an
        # actually-absent field is.
        databag = {**VALID_PROVIDER_DATABAG}
        del databag["prefix"]
        validator = _make_validator(databag, role=RelationRoleStub.provides)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "prefix_present")
        assert not check.passed

    def test_passes_schema_and_prefix_present_checks_with_empty_prefix(self) -> None:
        # An intentionally empty prefix (root of the keyspace) is a valid value: it must not
        # be rejected by the schema check (which would otherwise treat any falsy value as
        # "missing"), and no prefix_present failure should be raised either.
        databag = {**VALID_PROVIDER_DATABAG, "prefix": ""}
        validator = _make_validator(databag, role=RelationRoleStub.provides)

        result = validator.validate(level="simple")

        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed
        assert not any(c.name == "prefix_present" for c in result.checks)

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
