# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import base64
import json
from typing import cast
from unittest.mock import MagicMock, patch

import ops
import pytest

from validators.grafana_datasource.validator import (
    GrafanaDatasourceValidator,
    _resolve_admin_auth_headers,
)
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
    UnitStub,
)

# ---------------------------------------------------------------------------
# Helpers / factory
# ---------------------------------------------------------------------------


def _source_data(**overrides: object) -> str:
    data: dict[str, object] = {
        "model": "cos",
        "model_uuid": "0000-0000-0000-0000",
        "application": "prometheus",
        "type": "prometheus",
        "extra_fields": {},
        "secure_extra_fields": {},
    }
    data.update(overrides)
    return json.dumps(data)


VALID_DATABAG: dict[str, str] = {
    "grafana_source_data": _source_data(),
    "grafana_source_app_host": "http://prometheus.cos.svc.cluster.local:9090",
}


def _make_validator(
    app_databag: dict[str, str],
    endpoint: str = "grafana-source",
    role: RelationRoleStub = RelationRoleStub.requires,
    units: frozenset[UnitStub] = frozenset(),
    unit_data: dict[ApplicationStub | UnitStub | None, dict[str, str]] | None = None,
) -> GrafanaDatasourceValidator:
    app = ApplicationStub()
    data: dict[ApplicationStub | UnitStub | None, dict[str, str]] = {app: app_databag}
    if unit_data:
        data.update(unit_data)
    relation = RelationStub(name=endpoint, id=0, app=app, data=data, units=units)
    charm = make_charm_from_relation(relation, role=role, interface_name="grafana_datasource")
    return GrafanaDatasourceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


# ---------------------------------------------------------------------------
# Simple level tests
# ---------------------------------------------------------------------------


class TestGrafanaDatasourceValidatorSimple:
    @pytest.mark.parametrize(
        ("role", "should_skip"),
        [
            (RelationRoleStub.provides, True),
            (RelationRoleStub.peer, True),
            (RelationRoleStub.requires, False),
        ],
    )
    def test_skipped_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        validator = _make_validator(VALID_DATABAG, role=role)
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.return_value = (200, {"database": "ok"})
            result = validator.validate(level="simple")
        assert (result.status == "SKIPPED") == should_skip

    def test_skipped_for_unsupported_level(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        result = validator.validate(level="uat")
        assert result.status == "SKIPPED"

    def test_error_when_relation_app_databag_missing(self) -> None:
        app = ApplicationStub()
        relation = RelationStub(name="grafana-source", id=0, app=app, data={})
        del relation.data[app]
        charm = make_charm_from_relation(relation, interface_name="grafana_datasource")
        validator = GrafanaDatasourceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))
        result = validator.validate(level="simple")
        assert result.status == "ERROR"

    def test_fails_schema_when_source_data_missing(self) -> None:
        validator = _make_validator({})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        assert not next(c for c in result.checks if c.name == "schema").passed

    def test_fails_schema_when_source_data_not_json(self) -> None:
        validator = _make_validator({"grafana_source_data": "{not-json"})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        assert not next(c for c in result.checks if c.name == "schema").passed

    def test_fails_schema_when_required_fields_missing(self) -> None:
        databag = {"grafana_source_data": json.dumps({"model": "cos"})}
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "model_uuid" in schema_check.message

    def test_fails_url_when_no_host_published(self) -> None:
        databag = {"grafana_source_data": _source_data()}
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        assert not next(c for c in result.checks if c.name == "url").passed

    def test_falls_back_to_unit_host_when_app_host_missing(self) -> None:
        unit = UnitStub("prometheus/0")
        databag = {"grafana_source_data": _source_data()}
        validator = _make_validator(
            databag,
            units=frozenset({unit}),
            unit_data={unit: {"grafana_source_host": "http://10.0.0.5:9090"}},
        )
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.return_value = (200, {"database": "ok"})
            result = validator.validate(level="simple")
        url_check = next(c for c in result.checks if c.name == "url")
        assert url_check.passed
        assert result.status == "PASS"

    def test_fails_url_when_scheme_invalid(self) -> None:
        databag = {
            "grafana_source_data": _source_data(),
            "grafana_source_app_host": "ftp://prometheus.cos:9090",
        }
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        assert not next(c for c in result.checks if c.name == "url").passed

    def test_datasource_type_warns_but_passes_when_unrecognised(self) -> None:
        databag = {
            "grafana_source_data": _source_data(type="my-custom-plugin"),
            "grafana_source_app_host": "http://prometheus.cos:9090",
        }
        validator = _make_validator(databag)
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.return_value = (200, {"database": "ok"})
            result = validator.validate(level="simple")
        type_check = next(c for c in result.checks if c.name == "datasource_type")
        assert type_check.passed
        assert result.status == "PASS"

    def test_fails_auth_when_basic_auth_enabled_without_user(self) -> None:
        databag = {
            "grafana_source_data": _source_data(extra_fields={"basicAuth": True}),
            "grafana_source_app_host": "http://prometheus.cos:9090",
        }
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        auth_check = next(c for c in result.checks if c.name == "auth")
        assert not auth_check.passed
        assert "basicAuthUser" in auth_check.message

    def test_fails_auth_when_basic_auth_enabled_without_password(self) -> None:
        databag = {
            "grafana_source_data": _source_data(extra_fields={"basicAuth": True, "basicAuthUser": "admin"}),
            "grafana_source_app_host": "http://prometheus.cos:9090",
        }
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        auth_check = next(c for c in result.checks if c.name == "auth")
        assert not auth_check.passed
        assert "basicAuthPassword" in auth_check.message

    def test_passes_auth_with_complete_basic_auth_fields(self) -> None:
        databag = {
            "grafana_source_data": _source_data(
                extra_fields={"basicAuth": True, "basicAuthUser": "admin"},
                secure_extra_fields={"basicAuthPassword": "secret"},  # nosec B105
            ),
            "grafana_source_app_host": "http://prometheus.cos:9090",
        }
        validator = _make_validator(databag)
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.return_value = (200, {"database": "ok"})
            result = validator.validate(level="simple")
        auth_check = next(c for c in result.checks if c.name == "auth")
        assert auth_check.passed
        assert result.status == "PASS"

    def test_fails_when_grafana_health_endpoint_unreachable(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.side_effect = ConnectionRefusedError("connection refused")
            result = validator.validate(level="simple")
        assert result.status == "FAIL"
        assert not next(c for c in result.checks if c.name == "grafana_health").passed

    def test_passes_simple_with_valid_databag(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.return_value = (200, {"database": "ok", "version": "10.0.0"})
            result = validator.validate(level="simple")
        assert result.status == "PASS"
        mock_request.assert_called_once_with("GET", "/api/health")

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        validator = _make_validator(VALID_DATABAG, endpoint="grafana-source")
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.return_value = (200, {"database": "ok"})
            result = validator.validate(level="simple")
        assert result.endpoint == "grafana-source"
        assert result.interface == "grafana_datasource"
        assert result.role == "requires"


# ---------------------------------------------------------------------------
# Deep level tests
# ---------------------------------------------------------------------------


class TestGrafanaDatasourceValidatorDeep:
    def test_deep_registers_queries_health_and_cleans_up_datasource(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.side_effect = [
                (200, {"database": "ok"}),  # /api/health
                (200, {"uid": "canary-uid-123"}),  # POST /api/datasources
                (200, {"status": "OK"}),  # GET /api/datasources/uid/<uid>/health
                (200, {"message": "Datasource deleted"}),  # DELETE /api/datasources/uid/<uid>
            ]
            result = validator.validate(level="deep")

        assert result.status == "PASS"
        names = [c.name for c in result.checks]
        assert "register_datasource" in names
        assert "datasource_health" in names
        assert "cleanup_datasource" in names
        register_call, health_call, delete_call = mock_request.call_args_list[1:]
        assert register_call.args[0] == "POST"
        assert register_call.args[1] == "/api/datasources"
        assert health_call.args == ("GET", "/api/datasources/uid/canary-uid-123/health")
        assert delete_call.args == ("DELETE", "/api/datasources/uid/canary-uid-123")

    def test_deep_fails_when_registration_rejected(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.side_effect = [
                (200, {"database": "ok"}),
                (400, {"message": "datasource name already exists"}),
            ]
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        register_check = next(c for c in result.checks if c.name == "register_datasource")
        assert not register_check.passed
        assert not any(c.name == "datasource_health" for c in result.checks)

    def test_deep_still_cleans_up_when_datasource_health_check_fails(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.side_effect = [
                (200, {"database": "ok"}),
                (200, {"uid": "canary-uid-123"}),
                (200, {"status": "ERROR", "message": "datasource unreachable"}),
                (200, {"message": "Datasource deleted"}),
            ]
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        assert not next(c for c in result.checks if c.name == "datasource_health").passed
        assert next(c for c in result.checks if c.name == "cleanup_datasource").passed

    def test_deep_skipped_for_uat_level(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        result = validator.validate(level="uat")
        assert result.status == "SKIPPED"

    def test_deep_schema_failure_prevents_grafana_api_calls(self) -> None:
        validator = _make_validator({})
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        mock_request.assert_not_called()

    def test_deep_forwards_resolved_admin_headers_to_write_requests(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with (
            patch("validators.grafana_datasource.validator._grafana_request") as mock_request,
            patch(
                "validators.grafana_datasource.validator._resolve_admin_auth_headers",
                return_value={"Authorization": "Basic dXNlcjpwYXNz"},
            ),
        ):
            mock_request.side_effect = [
                (200, {"database": "ok"}),
                (200, {"uid": "canary-uid-123"}),
                (200, {"status": "OK"}),
                (200, {"message": "Datasource deleted"}),
            ]
            validator.validate(level="deep")

        _, register_kwargs = mock_request.call_args_list[1]
        assert register_kwargs["headers"] == {"Authorization": "Basic dXNlcjpwYXNz"}


# ---------------------------------------------------------------------------
# Admin credential resolution tests
# ---------------------------------------------------------------------------


class TestResolveAdminAuthHeaders:
    def test_returns_empty_dict_when_secret_lookup_fails(self) -> None:
        charm = MagicMock()
        charm.model.get_secret.side_effect = Exception("not found")
        assert _resolve_admin_auth_headers(charm) == {}

    def test_returns_empty_dict_when_secret_has_no_password(self) -> None:
        charm = MagicMock()
        charm.model.get_secret.return_value.get_content.return_value = {}
        assert _resolve_admin_auth_headers(charm) == {}

    def test_builds_basic_auth_header_with_default_username(self) -> None:
        charm = MagicMock()
        charm.model.get_secret.return_value.get_content.return_value = {"password": "secret"}  # nosec B105
        headers = _resolve_admin_auth_headers(charm)
        assert headers["Authorization"].startswith("Basic ")

    def test_uses_username_from_secret_when_present(self) -> None:
        charm = MagicMock()
        charm.model.get_secret.return_value.get_content.return_value = {
            "username": "custom-admin",
            "password": "secret",  # nosec B105
        }
        expected_token = base64.b64encode(b"custom-admin:secret").decode()
        headers = _resolve_admin_auth_headers(charm)
        assert headers["Authorization"] == f"Basic {expected_token}"
