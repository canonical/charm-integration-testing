# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from importlib.metadata import entry_points
from typing import cast
from unittest.mock import MagicMock, patch

import ops

from validators.smtp.validator import SmtpValidator, _enum_check, _port_check
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import ApplicationStub, RelationRoleStub, RelationStub

_INTERFACE = "smtp"
_ENDPOINT = "smtp"
VALID_DATABAG = {
    "host": "smtp.example.com",
    "port": "587",
    "auth_type": "none",
    "transport_security": "starttls",
}


def _make_validator(
    databag: dict[str, str], role: RelationRoleStub = RelationRoleStub.requires, remote_app: bool = True
) -> SmtpValidator:
    app = ApplicationStub() if remote_app else None
    relation = RelationStub(name=_ENDPOINT, id=0, app=app, data={})
    if app is not None:
        relation.data[app] = databag
    charm = make_charm_from_relation(relation, role=role, interface_name=_INTERFACE)
    return SmtpValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def test_validates_helper_values() -> None:
    assert _enum_check("auth_type", "plain", {"none", "plain"}).passed
    assert not _enum_check("auth_type", "bad", {"none", "plain"}).passed
    assert _port_check("25").passed
    assert not _port_check("70000").passed


def test_simple_pass() -> None:
    assert _make_validator(VALID_DATABAG).validate().status == "PASS"


def test_missing_fields_fail() -> None:
    result = _make_validator({"host": "smtp.example.com"}).validate()
    assert result.status == "FAIL"
    assert "port" in next(check for check in result.checks if check.name == "schema").message


def test_invalid_values_fail() -> None:
    result = _make_validator({**VALID_DATABAG, "auth_type": "invalid"}).validate()
    assert result.status == "FAIL"


def test_no_remote_app_is_error() -> None:
    assert _make_validator(VALID_DATABAG, remote_app=False).validate().status == "ERROR"


def test_unsupported_level_is_skipped() -> None:
    result = _make_validator(VALID_DATABAG).validate(level="uat")
    assert result.status == "SKIPPED"


def test_provider_role_is_skipped() -> None:
    result = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides).validate()
    assert result.status == "SKIPPED"


def test_plain_auth_requires_credentials() -> None:
    result = _make_validator({**VALID_DATABAG, "auth_type": "plain"}).validate()
    assert result.status == "FAIL"
    assert any(check.name == "credentials" for check in result.checks)


def test_deep_passes_smtp_handshake() -> None:
    client = MagicMock()
    client.__enter__.return_value = client
    with patch("validators.smtp.validator.smtplib.SMTP", return_value=client):
        result = _make_validator(VALID_DATABAG).validate(level="deep")
    assert result.status == "PASS"
    assert client.ehlo.call_count == 2


def test_deep_fails_unreachable_smtp() -> None:
    with patch("validators.smtp.validator.smtplib.SMTP", side_effect=OSError("connection refused")):
        result = _make_validator(VALID_DATABAG).validate(level="deep")
    assert result.status == "FAIL"
    assert "connection refused" in next(check for check in result.checks if check.name == "smtp_handshake").message


def test_entry_point_loads_validator() -> None:
    ep = next(ep for ep in entry_points(group="endpoint_validators") if ep.name == _INTERFACE)
    assert ep.load() is SmtpValidator
