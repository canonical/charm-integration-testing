# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import MagicMock, patch

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
                stored["value"] = fields.get(2, [b""])[0]  # type: ignore[assignment]
                return b""
            if method.endswith("/Range"):
                value = get_value() if callable(get_value) else get_value
                key_value_msg = _encode_bytes_field(1, b"canary-key") + _encode_bytes_field(5, value)
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
        ],
    )
    def test_fails_endpoints_format_check(self, bad_value: str, description: str) -> None:
        databag = {**VALID_REQUIRER_DATABAG, "endpoints": bad_value}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL", f"Expected FAIL for {description}"
        check = next(c for c in result.checks if c.name == "endpoints_format")
        assert not check.passed

    def test_fails_tls_ca_pem_check_when_invalid(self) -> None:
        databag = {**VALID_REQUIRER_DATABAG, "tls-ca": "not-a-pem"}
        validator = _make_validator(databag)

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "tls_ca_pem")
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

    def test_passes_with_secret_backed_credentials_and_local_secret_mtls(self) -> None:
        # GIVEN provider fields resolved via secrets rather than plaintext, matching
        # library revisions that publish username/uris/tls-ca as a secret group.
        databag = {
            "endpoints": VALID_REQUIRER_DATABAG["endpoints"],
            "version": VALID_REQUIRER_DATABAG["version"],
            "secret-user": "secret:etcd-user",
            "secret-tls": "secret:etcd-tls",
        }
        secrets = {
            "secret:etcd-user": {
                "username": VALID_REQUIRER_DATABAG["username"],
                "uris": VALID_REQUIRER_DATABAG["uris"],
            },
            "secret:etcd-tls": {"tls-ca": VALID_REQUIRER_DATABAG["tls-ca"]},
        }
        app = ApplicationStub()
        relation = RelationStub(name="etcd-client", id=0, app=app, data={app: databag})
        charm = make_charm_from_relation_and_secrets(relation, secrets, role=RelationRoleStub.requires)
        validator = EtcdClientValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        with patch("validators.etcd_client.validator.socket.create_connection") as mock_connect:
            mock_connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            result = validator.validate(level="simple")

        assert result.status == "PASS", result.checks
        assert charm.model.requested_ids == ["secret:etcd-user", "secret:etcd-tls"]


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
