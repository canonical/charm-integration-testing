# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from importlib.metadata import entry_points
from typing import cast
from unittest.mock import MagicMock, patch

import ops

from validators.saml.validator import SamlValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import ApplicationStub, RelationRoleStub, RelationStub

VALID_CERT = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"
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


def test_deep_metadata_passes() -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b"<EntityDescriptor/>"
    with patch("validators.saml.validator.urlopen", return_value=response):
        result = _make_validator({**VALID_DATA, "metadata_url": "https://idp.example.com/metadata"}).validate(
            level="deep"
        )
    assert result.status == "PASS"


def test_deep_metadata_fails() -> None:
    with patch("validators.saml.validator.urlopen", side_effect=OSError("unreachable")):
        result = _make_validator({**VALID_DATA, "metadata_url": "https://idp.example.com/metadata"}).validate(
            level="deep"
        )
    assert result.status == "FAIL"


def test_entry_point_loads_validator() -> None:
    ep = next(ep for ep in entry_points(group="endpoint_validators") if ep.name == "saml")
    assert ep.load() is SamlValidator
