# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from enum import Enum
from typing import cast
from unittest.mock import patch

import ops

from validators.trino_catalog.validator import TrinoCatalogValidator


class RelationRoleStub(Enum):
    requires = "requires"
    provides = "provides"
    peer = "peer"


class AppStub:
    pass


@dataclass
class RelationStub:
    name: str
    id: int
    app: AppStub | None
    data: dict[AppStub | None, dict[str, str]]


@dataclass
class RelationMetaStub:
    interface_name: str
    role: RelationRoleStub


@dataclass
class CharmMetaStub:
    relations: dict[str, RelationMetaStub]


@dataclass
class SecretStub:
    content: dict[str, str]

    def get_content(self) -> dict[str, str]:
        return self.content


@dataclass
class ModelStub:
    secrets: dict[str, dict[str, str]]

    def get_secret(self, id: str) -> SecretStub:  # noqa: A002
        return SecretStub(self.secrets[id])


@dataclass
class CharmStub:
    meta: CharmMetaStub
    model: ModelStub


@dataclass
class CursorStub:
    rows: list[tuple[str]] = field(default_factory=lambda: [("system",), ("sales",)])
    row: list[int] | None = field(default_factory=lambda: [1])
    error: Exception | None = None

    def execute(self, query: str) -> None:
        if self.error:
            raise self.error

    def fetchall(self) -> list[tuple[str]]:
        return self.rows

    def fetchone(self) -> list[int] | None:
        return self.row

    def __enter__(self) -> "CursorStub":
        return self

    def __exit__(self, *args: object) -> None:
        pass


@dataclass
class ConnectionStub:
    cursor_stub: CursorStub = field(default_factory=CursorStub)
    closed: bool = False

    def cursor(self) -> CursorStub:
        return self.cursor_stub

    def close(self) -> None:
        self.closed = True


VALID_DATABAG = {
    "trino_url": "trino-k8s.model.svc.cluster.local:8080",
    "trino_catalogs": '[{"name": "sales", "connector": "postgresql", "description": ""}]',
    "trino_credentials_secret_id": "secret:catalog",
}


def _make_validator(
    databag: dict[str, str],
    *,
    role: RelationRoleStub = RelationRoleStub.requires,
    secrets: dict[str, dict[str, str]] | None = None,
) -> TrinoCatalogValidator:
    remote_app = AppStub()
    relation = RelationStub(
        name="trino-catalog",
        id=1,
        app=remote_app,
        data={remote_app: databag},
    )
    charm = CharmStub(
        meta=CharmMetaStub(relations={relation.name: RelationMetaStub(interface_name="trino_catalog", role=role)}),
        model=ModelStub(
            secrets=(
                {"secret:catalog": {"username": "catalog-user", "password": "secret"}} if secrets is None else secrets
            )
        ),
    )
    return TrinoCatalogValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def test_simple_happy_path_passes() -> None:
    # GIVEN
    validator = _make_validator(VALID_DATABAG)
    connection = ConnectionStub()

    with patch(
        "validators.trino_catalog.validator.trino.dbapi.connect",
        return_value=connection,
    ):
        # WHEN
        result = validator.validate(level="simple")

    # THEN
    assert result.status == "PASS"
    assert {check.name for check in result.checks} == {
        "schema",
        "trino_url",
        "catalogs",
        "credentials",
        "connectivity",
    }
    assert connection.closed


def test_missing_fields_fail() -> None:
    # GIVEN
    validator = _make_validator({})

    # WHEN
    result = validator.validate(level="simple")

    # THEN
    assert result.status == "FAIL"
    assert result.checks[0].name == "schema"
    assert "trino_url" in result.checks[0].message


def test_no_remote_app_returns_error() -> None:
    # GIVEN
    relation = RelationStub(name="trino-catalog", id=1, app=None, data={})
    charm = CharmStub(
        meta=CharmMetaStub(
            relations={relation.name: RelationMetaStub(interface_name="trino_catalog", role=RelationRoleStub.requires)}
        ),
        model=ModelStub(secrets={}),
    )
    validator = TrinoCatalogValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

    # WHEN
    result = validator.validate(level="simple")

    # THEN
    assert result.status == "ERROR"


def test_unsupported_level_is_skipped() -> None:
    # GIVEN
    validator = _make_validator(VALID_DATABAG)

    # WHEN
    result = validator.validate(level="uat")

    # THEN
    assert result.status == "SKIPPED"


def test_provides_role_is_skipped() -> None:
    # GIVEN
    validator = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides)

    # WHEN
    result = validator.validate(level="simple")

    # THEN
    assert result.status == "SKIPPED"


def test_invalid_catalog_json_fails() -> None:
    # GIVEN
    validator = _make_validator({**VALID_DATABAG, "trino_catalogs": "not-json"})

    # WHEN
    result = validator.validate(level="simple")

    # THEN
    assert result.status == "FAIL"
    assert result.checks[-1].name == "catalogs"


def test_simple_fails_when_endpoint_is_unreachable() -> None:
    # GIVEN
    validator = _make_validator(VALID_DATABAG)

    with patch(
        "validators.trino_catalog.validator.trino.dbapi.connect",
        side_effect=ConnectionError("unreachable"),
    ):
        # WHEN
        result = validator.validate(level="simple")

    # THEN
    assert result.status == "FAIL"
    assert result.checks[-1].name == "connectivity"
    assert "unreachable" in result.checks[-1].message


def test_simple_rejects_explicit_port_zero() -> None:
    # GIVEN
    validator = _make_validator({**VALID_DATABAG, "trino_url": "trino.example.com:0"})

    with patch("validators.trino_catalog.validator.trino.dbapi.connect") as connect:
        # WHEN
        result = validator.validate(level="simple")

    # THEN
    assert result.status == "FAIL"
    assert result.checks[-1].name == "trino_url"
    assert "port must be between 1 and 65535" in result.checks[-1].message
    connect.assert_not_called()


def test_deep_defaults_portless_http_url_to_trino_port() -> None:
    # GIVEN
    validator = _make_validator({**VALID_DATABAG, "trino_url": "trino-k8s.model.svc.cluster.local"})
    connection = ConnectionStub()

    with patch(
        "validators.trino_catalog.validator.trino.dbapi.connect",
        return_value=connection,
    ) as connect:
        # WHEN
        result = validator.validate(level="deep")

    # THEN
    assert result.status == "PASS"
    assert connect.call_args.kwargs["port"] == 8080


def test_deep_infers_https_for_scheme_less_port_443_url() -> None:
    # GIVEN
    validator = _make_validator({**VALID_DATABAG, "trino_url": "trino.example.com:443"})
    connection = ConnectionStub()

    with patch(
        "validators.trino_catalog.validator.trino.dbapi.connect",
        return_value=connection,
    ) as connect:
        # WHEN
        result = validator.validate(level="deep")

    # THEN
    assert result.status == "PASS"
    assert connect.call_args.kwargs["http_scheme"] == "https"
    assert connect.call_args.kwargs["port"] == 443


def test_deep_queries_advertised_catalogs() -> None:
    # GIVEN
    validator = _make_validator(VALID_DATABAG)
    connection = ConnectionStub()
    auth = object()

    with (
        patch(
            "validators.trino_catalog.validator.trino.dbapi.connect",
            return_value=connection,
        ) as connect,
        patch(
            "validators.trino_catalog.validator.trino.auth.BasicAuthentication",
            return_value=auth,
        ) as basic_auth,
    ):
        # WHEN
        result = validator.validate(level="deep")

    # THEN
    assert result.status == "PASS"
    assert result.checks[-1].name == "catalog_query"
    assert connect.call_args.kwargs["user"] == "catalog-user"
    basic_auth.assert_called_once_with("catalog-user", "secret")
    assert connect.call_args.kwargs["auth"] is auth
    assert connection.closed


def test_deep_fails_when_advertised_catalog_is_missing() -> None:
    # GIVEN
    validator = _make_validator(VALID_DATABAG)
    connection = ConnectionStub(cursor_stub=CursorStub(rows=[("system",)]))

    with patch(
        "validators.trino_catalog.validator.trino.dbapi.connect",
        return_value=connection,
    ):
        # WHEN
        result = validator.validate(level="deep")

    # THEN
    assert result.status == "FAIL"
    assert "sales" in result.checks[-1].message


def test_deep_fails_when_query_raises() -> None:
    # GIVEN
    validator = _make_validator(VALID_DATABAG)
    connection = ConnectionStub(cursor_stub=CursorStub(error=ConnectionError("unreachable")))

    with patch(
        "validators.trino_catalog.validator.trino.dbapi.connect",
        return_value=connection,
    ):
        # WHEN
        result = validator.validate(level="deep")

    # THEN
    assert result.status == "FAIL"
    assert "unreachable" in result.checks[-1].message
    assert connection.closed
