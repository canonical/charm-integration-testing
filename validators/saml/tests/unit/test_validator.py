# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import base64
from datetime import datetime, timedelta, timezone
from importlib.metadata import entry_points
from typing import cast
from unittest.mock import MagicMock, patch

import ops
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from validators.saml.validator import SamlValidator, _NoRedirectHandler
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import ApplicationStub, RelationRoleStub, RelationStub


def _make_certificate() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.example.com")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


VALID_CERT = _make_certificate()
VALID_CERT_DER = base64.b64encode(
    x509.load_pem_x509_certificate(VALID_CERT.encode()).public_bytes(serialization.Encoding.DER)
).decode()
VALID_DATA = {
    "entity_id": "https://idp.example.com",
    "single_sign_on_service_redirect_url": "https://idp.example.com/sso",
    "single_sign_on_service_redirect_binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
    "x509certs": VALID_CERT,
}


def _make_validator(
    data: dict[str, str], role: RelationRoleStub = RelationRoleStub.requires, remote_app: bool = True
) -> SamlValidator:
    app = ApplicationStub() if remote_app else None
    relation = RelationStub(name="saml", id=0, app=app, data={})
    if app is not None:
        relation.data[app] = data
    charm = make_charm_from_relation(relation, role=role, interface_name="saml")
    return SamlValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def test_simple_passes() -> None:
    assert _make_validator(VALID_DATA).validate().status == "PASS"


def test_missing_fields_fail() -> None:
    assert _make_validator({}).validate().status == "FAIL"


def test_no_remote_app_errors() -> None:
    assert _make_validator(VALID_DATA, remote_app=False).validate().status == "ERROR"


def test_unsupported_level_skips() -> None:
    assert _make_validator(VALID_DATA).validate(level="uat").status == "SKIPPED"


def test_provider_role_skips() -> None:
    assert _make_validator(VALID_DATA, role=RelationRoleStub.provides).validate().status == "SKIPPED"


def test_unsupported_binding_fails() -> None:
    result = _make_validator({**VALID_DATA, "single_sign_on_service_redirect_binding": "invalid"}).validate()
    assert result.status == "FAIL"


def test_post_sso_endpoint_passes() -> None:
    data = {
        **VALID_DATA,
        "single_sign_on_service_redirect_url": "",
        "single_sign_on_service_redirect_binding": "",
        "single_sign_on_service_post_url": "https://idp.example.com/sso",
        "single_sign_on_service_post_binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
    }
    assert _make_validator(data).validate().status == "PASS"


def test_empty_certificates_are_optional() -> None:
    assert _make_validator({**VALID_DATA, "x509certs": ""}).validate().status == "PASS"


def test_sso_binding_must_match_endpoint_field() -> None:
    data = {
        **VALID_DATA,
        "single_sign_on_service_redirect_binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
    }
    assert _make_validator(data).validate().status == "FAIL"


def test_all_published_sso_endpoints_are_validated() -> None:
    data = {
        **VALID_DATA,
        "single_sign_on_service_post_url": "https://idp.example.com/sso",
        "single_sign_on_service_post_binding": "invalid",
    }
    assert _make_validator(data).validate().status == "FAIL"


def test_malformed_url_fails() -> None:
    result = _make_validator(
        {**VALID_DATA, "single_sign_on_service_redirect_url": "https://idp.example.com:bad"}
    ).validate()
    assert result.status == "FAIL"


def test_malformed_metadata_url_fails_at_simple_level() -> None:
    result = _make_validator({**VALID_DATA, "metadata_url": "https://idp.example.com:bad"}).validate()
    assert result.status == "FAIL"


def test_urn_entity_id_passes() -> None:
    result = _make_validator({**VALID_DATA, "entity_id": "urn:example:idp"}).validate()
    assert result.status == "PASS"


def test_scheme_only_entity_id_fails() -> None:
    result = _make_validator({**VALID_DATA, "entity_id": "https:"}).validate()
    assert result.status == "FAIL"


def test_empty_entity_id_component_fails() -> None:
    result = _make_validator({**VALID_DATA, "entity_id": "urn:"}).validate()
    assert result.status == "FAIL"


def test_entity_id_with_invalid_port_fails() -> None:
    result = _make_validator({**VALID_DATA, "entity_id": "https://idp.example.com:bad"}).validate()
    assert result.status == "FAIL"


def test_invalid_certificate_fails() -> None:
    result = _make_validator(
        {**VALID_DATA, "x509certs": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"}
    ).validate()
    assert result.status == "FAIL"


def test_base64_der_certificate_passes() -> None:
    result = _make_validator({**VALID_DATA, "x509certs": VALID_CERT_DER}).validate()
    assert result.status == "PASS"


def test_deep_metadata_passes() -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata"/>'
    with patch("validators.saml.validator._HTTP_OPENER.open", return_value=response):
        result = _make_validator({**VALID_DATA, "metadata_url": "https://idp.example.com/metadata"}).validate(
            level="deep"
        )
    assert result.status == "PASS"


def test_deep_metadata_fails() -> None:
    with patch("validators.saml.validator._HTTP_OPENER.open", side_effect=OSError("unreachable")):
        result = _make_validator({**VALID_DATA, "metadata_url": "https://idp.example.com/metadata"}).validate(
            level="deep"
        )
    assert result.status == "FAIL"


def test_metadata_opener_rejects_redirects() -> None:
    handler = _NoRedirectHandler()
    assert handler.redirect_request(None, None, 302, "Found", {}, "ftp://other.example") is None


def test_deep_metadata_rejects_malformed_url() -> None:
    with patch("validators.saml.validator._HTTP_OPENER.open") as open_url:
        result = _make_validator({**VALID_DATA, "metadata_url": "https://idp.example.com:bad"}).validate(level="deep")
    assert result.status == "FAIL"
    open_url.assert_not_called()


def test_deep_metadata_requires_entity_descriptor() -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b"<NotMetadata/>"
    with patch("validators.saml.validator._HTTP_OPENER.open", return_value=response):
        result = _make_validator({**VALID_DATA, "metadata_url": "https://idp.example.com/metadata"}).validate(
            level="deep"
        )
    assert result.status == "FAIL"


def test_deep_metadata_accepts_entities_descriptor() -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b"<EntitiesDescriptor xmlns='urn:oasis:names:tc:SAML:2.0:metadata'/>"
    with patch("validators.saml.validator._HTTP_OPENER.open", return_value=response):
        result = _make_validator({**VALID_DATA, "metadata_url": "https://idp.example.com/metadata"}).validate(
            level="deep"
        )
    assert result.status == "PASS"


def test_deep_metadata_requires_saml_namespace() -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b"<EntityDescriptor/>"
    with patch("validators.saml.validator._HTTP_OPENER.open", return_value=response):
        result = _make_validator({**VALID_DATA, "metadata_url": "https://idp.example.com/metadata"}).validate(
            level="deep"
        )
    assert result.status == "FAIL"


def test_deep_metadata_rejects_forbidden_xml() -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'<!DOCTYPE foo [<!ENTITY xxe "forbidden">]><EntityDescriptor>&xxe;</EntityDescriptor>'
    with patch("validators.saml.validator._HTTP_OPENER.open", return_value=response):
        result = _make_validator({**VALID_DATA, "metadata_url": "https://idp.example.com/metadata"}).validate(
            level="deep"
        )
    assert result.status == "FAIL"


def test_deep_metadata_rejects_oversized_response() -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b"x" * (1024 * 1024 + 1)
    with patch("validators.saml.validator._HTTP_OPENER.open", return_value=response):
        result = _make_validator({**VALID_DATA, "metadata_url": "https://idp.example.com/metadata"}).validate(
            level="deep"
        )
    assert result.status == "FAIL"


def test_entry_point_loads_validator() -> None:
    ep = next(ep for ep in entry_points(group="endpoint_validators") if ep.name == "saml")
    assert ep.load() is SamlValidator
