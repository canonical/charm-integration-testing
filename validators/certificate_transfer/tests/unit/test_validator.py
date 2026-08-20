# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from datetime import datetime, timedelta, timezone
from typing import cast

import ops
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from validators.certificate_transfer.validator import CertificateTransferValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
    UnitStub,
)

# ---------------------------------------------------------------------------
# Certificate fixtures
# ---------------------------------------------------------------------------


def _make_certificate_pem(*, not_valid_before: datetime, not_valid_after: datetime) -> str:
    """Build a minimal self-signed X.509 certificate PEM for tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "validator-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before)
        .not_valid_after(not_valid_after)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(encoding=serialization.Encoding.PEM).decode()


VALID_CERT_PEM = _make_certificate_pem(
    not_valid_before=datetime.now(timezone.utc) - timedelta(days=1),
    not_valid_after=datetime.now(timezone.utc) + timedelta(days=365),
)
EXPIRED_CERT_PEM = _make_certificate_pem(
    not_valid_before=datetime.now(timezone.utc) - timedelta(days=2),
    not_valid_after=datetime.now(timezone.utc) - timedelta(days=1),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_validator(
    app_databag: dict[str, str] | None = None,
    unit_databags: dict[UnitStub, dict[str, str]] | None = None,
    endpoint: str = "receive-ca-cert",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> CertificateTransferValidator:
    app = ApplicationStub()
    data: dict[ApplicationStub | UnitStub | None, dict[str, str]] = {app: app_databag or {}}
    units: frozenset[UnitStub] = frozenset()
    if unit_databags:
        for unit, unit_databag in unit_databags.items():
            data[unit] = unit_databag
        units = frozenset(unit_databags.keys())
    relation = RelationStub(app=app, data=data, name=endpoint, id=0, units=units)
    charm = make_charm_from_relation(relation, interface_name="certificate_transfer", role=role)
    return CertificateTransferValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def _v1_databag(*certs: str) -> dict[str, str]:
    return {"certificates": json.dumps(list(certs)), "version": "1"}


def _v0_unit_databag(cert: str) -> dict[str, str]:
    """Build a v0 unit databag matching real provider charms: plain (non-JSON) PEM strings."""
    return {"ca": cert, "certificate": cert, "version": "0"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCertificateTransferValidatorSimple:
    @pytest.mark.parametrize(
        "role,should_skip",
        [(RelationRoleStub.requires, False), (RelationRoleStub.provides, True), (RelationRoleStub.peer, True)],
    )
    def test_returns_skipped_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN
        validator = _make_validator(_v1_databag(VALID_CERT_PEM), role=role)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

    def test_returns_skipped_for_unsupported_level(self) -> None:
        # GIVEN
        validator = _make_validator(_v1_databag(VALID_CERT_PEM))

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None
        assert "not supported" in result.error

    def test_returns_error_when_no_remote_app(self) -> None:
        # GIVEN a relation with no data for the remote app (relation.app is None)
        relation = RelationStub(name="receive-ca-cert", id=0, app=None, data={})
        charm = make_charm_from_relation(relation, interface_name="certificate_transfer")
        validator = CertificateTransferValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"
        assert result.error is not None
        assert "No remote application" in result.error

    def test_fails_schema_check_when_no_certificates_present(self) -> None:
        # GIVEN a databag with no certificate data at all
        validator = _make_validator({"version": "1"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed

    def test_passes_with_v1_app_databag_certificates(self) -> None:
        # GIVEN a v1-style provider app databag with a valid certificate
        validator = _make_validator(_v1_databag(VALID_CERT_PEM))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed
        parseable_check = next(c for c in result.checks if c.name == "parseable")
        assert parseable_check.passed
        validity_check = next(c for c in result.checks if c.name == "validity_period")
        assert validity_check.passed

    def test_passes_with_v0_unit_databag_certificates(self) -> None:
        # GIVEN a v0-style fallback where certs live in the related unit's databag
        unit = UnitStub("self-signed-certificates/0")
        validator = _make_validator({"version": "0"}, unit_databags={unit: _v0_unit_databag(VALID_CERT_PEM)})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed
        assert "1 certificate" in schema_check.message

    def test_dedupes_identical_certificates_across_ca_and_certificate_fields(self) -> None:
        # GIVEN a v0 unit databag where 'ca' and 'certificate' hold the same cert (self-signed case)
        unit = UnitStub("self-signed-certificates/0")
        validator = _make_validator(unit_databags={unit: _v0_unit_databag(VALID_CERT_PEM)})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert "1 certificate" in schema_check.message

    def test_passes_with_json_encoded_v0_unit_fields(self) -> None:
        # GIVEN a v0 unit databag where 'ca'/'certificate' are (non-standard) JSON-encoded strings
        unit = UnitStub("self-signed-certificates/0")
        databag = {"ca": json.dumps(VALID_CERT_PEM), "chain": json.dumps([]), "version": "0"}
        validator = _make_validator(unit_databags={unit: databag})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"

    def test_passes_with_real_self_signed_certificates_databag_shape(self) -> None:
        # GIVEN a databag matching the actual self-signed-certificates charm output: a plain
        # (non-JSON) PEM 'ca' field, no 'certificate' field, and an empty JSON-encoded 'chain'
        unit = UnitStub("self-signed-certificates/0")
        databag = {"ca": VALID_CERT_PEM, "chain": "[]", "version": "0"}
        validator = _make_validator(unit_databags={unit: databag})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert "1 certificate" in schema_check.message

    def test_fails_parseable_check_on_malformed_certificate(self) -> None:
        # GIVEN a databag with a certificate field that isn't valid PEM
        validator = _make_validator(_v1_databag("not-a-real-certificate"))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        parseable_check = next(c for c in result.checks if c.name == "parseable")
        assert not parseable_check.passed

    def test_fails_schema_check_on_invalid_json(self) -> None:
        # GIVEN a databag whose 'certificates' field is not valid JSON
        validator = _make_validator({"certificates": "{not-json", "version": "1"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "Invalid JSON" in schema_check.message

    def test_fails_validity_period_check_on_expired_certificate(self) -> None:
        # GIVEN a databag with an expired certificate
        validator = _make_validator(_v1_databag(EXPIRED_CERT_PEM))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        validity_check = next(c for c in result.checks if c.name == "validity_period")
        assert not validity_check.passed

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(_v1_databag(VALID_CERT_PEM), endpoint="my-endpoint")

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-endpoint"
        assert result.interface == "certificate_transfer"


class TestCertificateTransferValidatorDeep:
    def test_returns_skipped_for_uat_level(self) -> None:
        # GIVEN
        validator = _make_validator(_v1_databag(VALID_CERT_PEM))

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_deep_passes_and_loads_trust_store(self) -> None:
        # GIVEN a valid certificate transferred via the v1 app databag
        validator = _make_validator(_v1_databag(VALID_CERT_PEM))

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        assert result.level == "deep"
        trust_store_check = next(c for c in result.checks if c.name == "trust_store_load")
        assert trust_store_check.passed

    def test_deep_fails_schema_before_attempting_trust_store_load(self) -> None:
        # GIVEN no certificates at all
        validator = _make_validator({"version": "1"})

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        assert not any(c.name == "trust_store_load" for c in result.checks)

    def test_deep_fails_trust_store_load_on_malformed_pem(self) -> None:
        # GIVEN certificate data that fails to parse as X.509 (caught before trust-store load)
        validator = _make_validator(_v1_databag("not-a-real-certificate"))

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        assert not any(c.name == "trust_store_load" for c in result.checks)
