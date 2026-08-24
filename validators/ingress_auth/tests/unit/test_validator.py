# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import yaml

from validators.ingress_auth.validator import IngressAuthValidator


@dataclass(frozen=True)
class AppStub:
    name: str = "provider"


@dataclass(frozen=True)
class UnitStub:
    name: str


@dataclass
class RelationStub:
    name: str
    id: int
    app: AppStub | None
    data: dict[Any, dict[str, str]] = field(default_factory=dict)
    units: frozenset[UnitStub] = field(default_factory=frozenset)


@dataclass
class RelationMetaStub:
    interface_name: str
    role: Any


@dataclass
class CharmMetaStub:
    relations: dict[str, RelationMetaStub]


@dataclass
class CharmStub:
    meta: CharmMetaStub
    model: Any


@dataclass
class _RoleStub:
    value: str


@dataclass
class _ModelStub:
    name: str = "test-model"


def _make_validator(
    *,
    app_databag: dict[str, str] | None = None,
    unit_databags: list[dict[str, str]] | None = None,
    app_present: bool = True,
    role: str = "requires",
) -> IngressAuthValidator:
    endpoint = "ingress-auth"
    app = AppStub() if app_present else None
    relation_data: dict[Any, dict[str, str]] = {}
    if app is not None:
        relation_data[app] = app_databag or {}

    units: list[UnitStub] = []
    for index, unit_databag in enumerate(unit_databags or []):
        unit = UnitStub(name=f"provider/{index}")
        units.append(unit)
        relation_data[unit] = unit_databag

    relation = RelationStub(
        name=endpoint,
        id=1,
        app=app,
        data=relation_data,
        units=frozenset(units),
    )
    relation_meta = RelationMetaStub(interface_name="ingress-auth", role=_RoleStub(role))
    charm = CharmStub(
        meta=CharmMetaStub(relations={endpoint: relation_meta}),
        model=_ModelStub(),
    )
    return IngressAuthValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def test_happy_path_passes() -> None:
    validator = _make_validator(
        app_databag={"_supported_versions": yaml.safe_dump(["v1"])},
        unit_databags=[
            {
                "ingress-address": "10.0.0.1",
                "private-address": "10.0.0.1",
                "egress-subnets": "10.152.183.0/24",
            }
        ],
    )
    result = validator.validate(level="simple")
    assert result.status == "PASS"


def test_missing_fields_fails() -> None:
    validator = _make_validator(
        app_databag={"_supported_versions": yaml.safe_dump(["v1"])},
        unit_databags=[
            {
                "ingress-address": "10.0.0.1",
                "egress-subnets": "10.152.183.0/24",
            }
        ],
    )
    result = validator.validate(level="simple")
    assert result.status == "FAIL"
    check = next(check for check in result.checks if check.name == "unit_databag")
    assert "private-address" in check.message


def test_missing_supported_versions_fails() -> None:
    validator = _make_validator(
        app_databag={},
        unit_databags=[
            {
                "ingress-address": "10.0.0.1",
                "private-address": "10.0.0.1",
                "egress-subnets": "10.152.183.0/24",
            }
        ],
    )
    result = validator.validate(level="simple")
    assert result.status == "FAIL"
    check = next(check for check in result.checks if check.name == "version")
    assert "_supported_versions" in check.message


def test_unsupported_version_fails() -> None:
    validator = _make_validator(
        app_databag={"_supported_versions": yaml.safe_dump(["v2"])},
        unit_databags=[
            {
                "ingress-address": "10.0.0.1",
                "private-address": "10.0.0.1",
                "egress-subnets": "10.152.183.0/24",
            }
        ],
    )
    result = validator.validate(level="simple")
    assert result.status == "FAIL"
    check = next(check for check in result.checks if check.name == "version")
    assert "does not advertise 'v1'" in check.message


def test_no_app_returns_error() -> None:
    validator = _make_validator(app_present=False, unit_databags=[])
    result = validator.validate(level="simple")
    assert result.status == "ERROR"


def test_unsupported_role_without_app_is_skipped() -> None:
    validator = _make_validator(role="peer", app_present=False, unit_databags=[])
    result = validator.validate(level="simple")
    assert result.status == "SKIPPED"


def test_unsupported_level_is_skipped() -> None:
    validator = _make_validator(app_databag={}, unit_databags=[])
    result = validator.validate(level="deep")
    assert result.status == "SKIPPED"


def test_provider_validates_requirer_databag() -> None:
    validator = _make_validator(
        role="provides",
        app_databag={
            "_supported_versions": yaml.safe_dump(["v1"]),
            "data": yaml.safe_dump(
                {
                    "service": "oidc-gatekeeper",
                    "port": 8080,
                    "allowed-request-headers": ["cookie"],
                    "allowed-response-headers": ["kubeflow-userid"],
                }
            ),
        },
    )
    result = validator.validate(level="simple")
    assert result.status == "PASS"


def test_provider_missing_data_key_fails() -> None:
    validator = _make_validator(
        role="provides",
        app_databag={"_supported_versions": yaml.safe_dump(["v1"])},
    )
    result = validator.validate(level="simple")
    assert result.status == "FAIL"
    check = next(check for check in result.checks if check.name == "schema")
    assert "Missing 'data' key" in check.message


def test_provider_missing_supported_versions_fails() -> None:
    validator = _make_validator(
        role="provides",
        app_databag={"data": yaml.safe_dump({"service": "oidc-gatekeeper", "port": 8080})},
    )
    result = validator.validate(level="simple")
    assert result.status == "FAIL"
    check = next(check for check in result.checks if check.name == "version")
    assert "_supported_versions" in check.message
    assert not any(check.name == "schema" for check in result.checks)


def test_provider_unsupported_version_fails() -> None:
    validator = _make_validator(
        role="provides",
        app_databag={
            "_supported_versions": yaml.safe_dump(["v2"]),
            "data": yaml.safe_dump({"service": "oidc-gatekeeper", "port": 8080}),
        },
    )
    result = validator.validate(level="simple")
    assert result.status == "FAIL"
    check = next(check for check in result.checks if check.name == "version")
    assert "does not advertise 'v1'" in check.message
    assert not any(check.name == "schema" for check in result.checks)


def test_provider_rejects_invalid_contract() -> None:
    validator = _make_validator(
        role="provides",
        app_databag={
            "_supported_versions": yaml.safe_dump(["v1"]),
            "data": yaml.safe_dump({"service": "oidc-gatekeeper", "port": "not-a-port"}),
        },
    )
    result = validator.validate(level="simple")
    assert result.status == "FAIL"
    check = next(check for check in result.checks if check.name == "port")
    assert "integer" in check.message


def test_provider_rejects_non_string_service() -> None:
    validator = _make_validator(
        role="provides",
        app_databag={
            "_supported_versions": yaml.safe_dump(["v1"]),
            "data": yaml.safe_dump({"service": ["oidc-gatekeeper"], "port": 8080}),
        },
    )
    result = validator.validate(level="simple")
    assert result.status == "FAIL"
    check = next(check for check in result.checks if check.name == "required_fields")
    assert "'service' must be a string" in check.message


def test_provider_rejects_boolean_port() -> None:
    validator = _make_validator(
        role="provides",
        app_databag={
            "_supported_versions": yaml.safe_dump(["v1"]),
            "data": yaml.safe_dump({"service": "oidc-gatekeeper", "port": True}),
        },
    )
    result = validator.validate(level="simple")
    assert result.status == "FAIL"
    check = next(check for check in result.checks if check.name == "port")
    assert "integer" in check.message


def test_provider_rejects_float_port() -> None:
    validator = _make_validator(
        role="provides",
        app_databag={
            "_supported_versions": yaml.safe_dump(["v1"]),
            "data": yaml.safe_dump({"service": "oidc-gatekeeper", "port": 1.5}),
        },
    )
    result = validator.validate(level="simple")
    assert result.status == "FAIL"
    check = next(check for check in result.checks if check.name == "port")
    assert "integer" in check.message


def test_provider_deep_connectivity_passes() -> None:
    validator = _make_validator(
        role="provides",
        app_databag={
            "_supported_versions": yaml.safe_dump(["v1"]),
            "data": yaml.safe_dump({"service": "oidc-gatekeeper", "port": 8080}),
        },
    )
    with patch("validators.ingress_auth.validator.socket.create_connection") as mock_connect:
        mock_connect.return_value.__enter__ = lambda self: None
        mock_connect.return_value.__exit__ = lambda self, *args: None
        result = validator.validate(level="deep")
    mock_connect.assert_called_once_with(("oidc-gatekeeper.test-model.svc.cluster.local", 8080), timeout=5)
    assert result.status == "PASS"
    check = next(check for check in result.checks if check.name == "connectivity")
    assert "TCP reached" in check.message


def test_provider_deep_connectivity_fails() -> None:
    validator = _make_validator(
        role="provides",
        app_databag={
            "_supported_versions": yaml.safe_dump(["v1"]),
            "data": yaml.safe_dump({"service": "oidc-gatekeeper", "port": 8080}),
        },
    )
    with patch("validators.ingress_auth.validator.socket.create_connection", side_effect=OSError("refused")):
        result = validator.validate(level="deep")
    assert result.status == "FAIL"
    check = next(check for check in result.checks if check.name == "connectivity")
    assert "refused" in check.message


def test_provider_deep_skips_connectivity_when_port_invalid() -> None:
    validator = _make_validator(
        role="provides",
        app_databag={
            "_supported_versions": yaml.safe_dump(["v1"]),
            "data": yaml.safe_dump({"service": "oidc-gatekeeper", "port": "not-a-port"}),
        },
    )
    with patch("validators.ingress_auth.validator.socket.create_connection") as mock_connect:
        result = validator.validate(level="deep")
    mock_connect.assert_not_called()
    assert result.status == "FAIL"
    assert not any(check.name == "connectivity" for check in result.checks)
