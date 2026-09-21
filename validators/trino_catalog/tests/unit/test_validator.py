# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from typing import cast
from unittest.mock import patch

import ops
import pytest

from validators.test_utils.helpers import make_charm_from_relation, make_charm_from_relation_and_secrets
from validators.test_utils.stubs import ApplicationStub, RelationRoleStub, RelationStub
from validators.trino_catalog.validator import (
    TrinoCatalogValidator,
    TrinoConnectionInfo,
    _connect,
    _parse_trino_url,
)


@dataclass(frozen=True)
class CatalogValidationCase:
    value: str
    expected_message: str


@dataclass(frozen=True)
class InvalidUrlCase:
    value: str
    expected_message: str


@dataclass
class CursorStub:
    rows: list[tuple[str]] = field(default_factory=lambda: [("system",), ("sales",)])
    row: list[int] | None = field(default_factory=lambda: [1])
    error: Exception | None = None
    executed_queries: list[str] = field(default_factory=list)

    def execute(self, query: str) -> None:
        self.executed_queries.append(query)
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
    "trino_url": "https://trino.example.com:443",
    "trino_catalogs": '[{"name": "sales", "connector": "postgresql", "description": ""}]',
    "trino_credentials_secret_id": "secret:catalog",
}


def _make_validator(
    databag: dict[str, str],
    *,
    role: RelationRoleStub = RelationRoleStub.requires,
    secrets: dict[str, dict[str, str]] | None = None,
) -> TrinoCatalogValidator:
    remote_app = ApplicationStub()
    relation = RelationStub(
        name="trino-catalog",
        id=1,
        app=remote_app,
        data={remote_app: databag},
    )
    charm = make_charm_from_relation_and_secrets(
        relation,
        {"secret:catalog": {"username": "catalog-user", "password": "secret"}} if secrets is None else secrets,
        role=role,
    )
    charm.meta.relations[relation.name].interface_name = "trino_catalog"
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
        "trino_catalogs",
        "credentials",
        "connectivity",
    }
    assert connection.cursor_stub.executed_queries == ["SELECT 1"]
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
    charm = make_charm_from_relation(relation, interface_name="trino_catalog")
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
    assert result.checks[-1].name == "trino_catalogs"


@pytest.mark.parametrize(
    "case",
    [
        CatalogValidationCase("{}", "trino_catalogs must be a JSON list."),
        CatalogValidationCase("[{}]", "Catalog at index 0 must contain a string name."),
        CatalogValidationCase('[{"name": 1}]', "Catalog at index 0 must contain a string name."),
        CatalogValidationCase('[{"name": "  "}]', "Catalog at index 0 has an empty name."),
        CatalogValidationCase(
            '[{"name": "sales", "connector": 1}]',
            "Catalog 'sales' field 'connector' must be a string.",
        ),
        CatalogValidationCase(
            '[{"name": "sales", "description": 1}]',
            "Catalog 'sales' field 'description' must be a string.",
        ),
        CatalogValidationCase(
            '[{"name": "sales"}, {"name": "sales"}]',
            "trino_catalogs contains duplicate names.",
        ),
    ],
)
def test_invalid_catalog_schema_fails(case: CatalogValidationCase) -> None:
    # GIVEN
    validator = _make_validator({**VALID_DATABAG, "trino_catalogs": case.value})

    # WHEN
    result = validator.validate(level="simple")

    # THEN
    assert result.status == "FAIL"
    assert result.checks[-1].name == "trino_catalogs"
    assert result.checks[-1].message == case.expected_message


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


def test_simple_redacts_malformed_port_from_diagnostic() -> None:
    # GIVEN
    validator = _make_validator({**VALID_DATABAG, "trino_url": "http://admin:hunter2"})

    with patch("validators.trino_catalog.validator.trino.dbapi.connect") as connect:
        # WHEN
        result = validator.validate(level="simple")

    # THEN
    assert result.status == "FAIL"
    assert result.checks[-1].name == "trino_url"
    assert result.checks[-1].message == "Invalid trino_url: port must be an integer between 1 and 65535"
    assert "hunter2" not in result.checks[-1].message
    connect.assert_not_called()


def test_simple_redacts_urlsplit_error_from_diagnostic() -> None:
    # GIVEN
    validator = _make_validator({**VALID_DATABAG, "trino_url": "http://admin:hunter2\uff20trino.example"})

    with patch("validators.trino_catalog.validator.trino.dbapi.connect") as connect:
        # WHEN
        result = validator.validate(level="simple")

    # THEN
    assert result.status == "FAIL"
    assert result.checks[-1].name == "trino_url"
    assert result.checks[-1].message == "Invalid trino_url: URL could not be parsed"
    assert "admin" not in result.checks[-1].message
    assert "hunter2" not in result.checks[-1].message
    connect.assert_not_called()


@pytest.mark.parametrize(
    "case",
    [
        InvalidUrlCase("http://@trino.example:8080", "URL must contain only a scheme, hostname, and port"),
        InvalidUrlCase("trino.example:8080?", "URL must contain only a scheme, hostname, and port"),
        InvalidUrlCase("trino.example:8080#", "URL must contain only a scheme, hostname, and port"),
    ],
)
def test_simple_rejects_empty_forbidden_url_components(case: InvalidUrlCase) -> None:
    # GIVEN
    validator = _make_validator({**VALID_DATABAG, "trino_url": case.value})

    with patch("validators.trino_catalog.validator.trino.dbapi.connect") as connect:
        # WHEN
        result = validator.validate(level="simple")

    # THEN
    assert result.status == "FAIL"
    assert result.checks[-1].name == "trino_url"
    assert case.expected_message in result.checks[-1].message
    connect.assert_not_called()


def test_portless_url_defaults_to_http_trino_port() -> None:
    # WHEN
    connection_info, check = _parse_trino_url("trino-k8s.model.svc.cluster.local")

    # THEN
    assert check.passed
    assert connection_info == TrinoConnectionInfo(
        host="trino-k8s.model.svc.cluster.local",
        port=8080,
        http_scheme="http",
    )


def test_deep_rejects_portless_http_url_without_sending_credentials() -> None:
    # GIVEN
    validator = _make_validator({**VALID_DATABAG, "trino_url": "trino-k8s.model.svc.cluster.local"})

    with patch(
        "validators.trino_catalog.validator.trino.dbapi.connect",
    ) as connect:
        # WHEN
        result = validator.validate(level="deep")

    # THEN
    assert result.status == "FAIL"
    assert result.checks[-1].name == "catalog_query"
    assert "requires HTTPS" in result.checks[-1].message
    connect.assert_not_called()


def test_deep_infers_https_for_scheme_less_port_443_url() -> None:
    # GIVEN
    validator = _make_validator({**VALID_DATABAG, "trino_url": "trino.example.com:443"})
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
    assert connect.call_args.kwargs["http_scheme"] == "https"
    assert connect.call_args.kwargs["port"] == 443
    basic_auth.assert_called_once_with("catalog-user", "secret")
    assert connect.call_args.kwargs["auth"] is auth
    assert "allow_insecure_auth" not in connect.call_args.kwargs


def test_connect_rejects_authenticated_internal_http_connection() -> None:
    # GIVEN
    connection_info = TrinoConnectionInfo(
        host="trino-k8s.model.svc.cluster.local",
        port=8080,
        http_scheme="http",
    )

    with (
        patch("validators.trino_catalog.validator.trino.dbapi.connect") as connect,
        patch("validators.trino_catalog.validator.trino.auth.BasicAuthentication") as basic_auth,
    ):
        # WHEN
        with pytest.raises(ValueError, match="requires HTTPS"):
            _connect(
                connection_info,
                {"username": "catalog-user", "password": "secret"},
            )

    # THEN
    basic_auth.assert_not_called()
    connect.assert_not_called()


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
    assert "allow_insecure_auth" not in connect.call_args.kwargs
    assert connection.cursor_stub.executed_queries == ["SHOW CATALOGS"]
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


def test_deep_preserves_advertised_catalog_name_whitespace() -> None:
    # GIVEN
    validator = _make_validator({**VALID_DATABAG, "trino_catalogs": '[{"name": " sales "}]'})
    connection = ConnectionStub(cursor_stub=CursorStub(rows=[("sales",)]))

    with patch(
        "validators.trino_catalog.validator.trino.dbapi.connect",
        return_value=connection,
    ):
        # WHEN
        result = validator.validate(level="deep")

    # THEN
    assert result.status == "FAIL"
    assert " sales " in result.checks[-1].message


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
