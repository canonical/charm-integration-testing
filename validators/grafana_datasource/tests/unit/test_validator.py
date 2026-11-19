# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import base64
import json
from dataclasses import dataclass
from typing import cast
from unittest.mock import MagicMock, call, patch

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


@dataclass(frozen=True)
class AlertmanagerImplementationParams:
    implementation: str | None
    status_path: str


@dataclass(frozen=True)
class AlertmanagerResponseParams:
    status: int
    payload: object


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
    @pytest.mark.parametrize(
        "extra_fields",
        [
            pytest.param(["invalid"], id="json-array"),
            pytest.param("invalid", id="json-string"),
        ],
    )
    def test_deep_rejects_non_object_extra_fields_before_requests(self, extra_fields: object) -> None:
        # GIVEN malformed Alertmanager metadata.
        validator = _make_validator(
            {
                "grafana_source_data": _source_data(type="alertmanager", extra_fields=extra_fields),
                "grafana_source_app_host": "http://alertmanager.cos:9093",
            }
        )
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            # WHEN deep validation runs.
            result = validator.validate(level="deep")

        # THEN metadata validation fails before any Grafana requests or canary creation.
        assert result.status == "FAIL"
        assert not next(c for c in result.checks if c.name == "auth").passed
        mock_request.assert_not_called()

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

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param(AlertmanagerImplementationParams("prometheus", "/api/v2/status"), id="prometheus"),
            pytest.param(AlertmanagerImplementationParams("mimir", "/alertmanager/api/v2/status"), id="mimir"),
            pytest.param(AlertmanagerImplementationParams("cortex", "/alertmanager/api/v2/status"), id="cortex"),
            pytest.param(AlertmanagerImplementationParams(None, "/alertmanager/api/v2/status"), id="default-mimir"),
        ],
    )
    def test_deep_checks_alertmanager_through_grafana_proxy(self, params: AlertmanagerImplementationParams) -> None:
        # GIVEN an Alertmanager relation and an authenticated Grafana API.
        extra_fields = {"implementation": params.implementation} if params.implementation else None
        validator = _make_validator(
            {
                "grafana_source_data": _source_data(type="alertmanager", extra_fields=extra_fields),
                "grafana_source_app_host": "http://alertmanager.cos:9093",
            }
        )
        admin_headers = {"Authorization": "Basic dXNlcjpwYXNz"}
        with (
            patch("validators.grafana_datasource.validator._grafana_request") as mock_request,
            patch(
                "validators.grafana_datasource.validator._resolve_admin_auth_headers",
                return_value=admin_headers,
            ),
        ):
            mock_request.side_effect = [
                (200, {"database": "ok"}),
                (200, {"uid": "canary-uid-123"}),
                (200, {"versionInfo": {"version": "0.31.1"}, "config": {"original": "private-config"}}),
                (200, {"message": "Datasource deleted"}),
            ]

            # WHEN deep validation runs.
            result = validator.validate(level="deep")

        # THEN Grafana proxies the status check and the canary is removed.
        assert result.status == "PASS"
        mock_request.assert_has_calls(
            [
                call(
                    "GET",
                    f"/api/datasources/proxy/uid/canary-uid-123{params.status_path}",
                    headers=admin_headers,
                ),
                call("DELETE", "/api/datasources/uid/canary-uid-123", headers=admin_headers),
            ]
        )
        health_check = next(c for c in result.checks if c.name == "datasource_health")
        assert "0.31.1" in health_check.message
        assert "private-config" not in health_check.message

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param(
                AlertmanagerResponseParams(
                    502, {"message": "private-config", "config": {"original": "private-config"}}
                ),
                id="upstream-unreachable",
            ),
            pytest.param(AlertmanagerResponseParams(401, {"message": "private-config"}), id="unauthorized"),
            pytest.param(AlertmanagerResponseParams(403, {"message": "private-config"}), id="forbidden"),
            pytest.param(AlertmanagerResponseParams(200, {"status": "OK"}), id="not-alertmanager-status"),
            pytest.param(AlertmanagerResponseParams(200, {"versionInfo": {"version": ""}}), id="missing-version"),
            pytest.param(AlertmanagerResponseParams(200, []), id="json-array"),
            pytest.param(AlertmanagerResponseParams(200, None), id="json-null"),
            pytest.param(AlertmanagerResponseParams(200, "OK"), id="json-string"),
            pytest.param(AlertmanagerResponseParams(200, 1), id="json-number"),
        ],
    )
    def test_deep_fails_and_cleans_up_when_alertmanager_status_is_invalid(
        self, params: AlertmanagerResponseParams
    ) -> None:
        # GIVEN an Alertmanager datasource that does not return a valid status.
        validator = _make_validator(
            {
                "grafana_source_data": _source_data(type="alertmanager", extra_fields={"implementation": "prometheus"}),
                "grafana_source_app_host": "http://alertmanager.cos:9093",
            }
        )
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.side_effect = [
                (200, {"database": "ok"}),
                (200, {"uid": "canary-uid-123"}),
                (params.status, params.payload),
                (200, {"message": "Datasource deleted"}),
            ]

            # WHEN deep validation runs.
            result = validator.validate(level="deep")

        # THEN an unsuccessful proxy check fails validation without leaking the canary.
        assert result.status == "FAIL"
        health_check = next(c for c in result.checks if c.name == "datasource_health")
        assert not health_check.passed
        assert f"HTTP {params.status}" in health_check.message
        assert "private-config" not in result.model_dump_json()
        assert next(c for c in result.checks if c.name == "cleanup_datasource").passed

    def test_deep_cleans_up_when_alertmanager_proxy_request_raises(self) -> None:
        # GIVEN a transport error while checking Alertmanager through Grafana.
        validator = _make_validator(
            {
                "grafana_source_data": _source_data(type="alertmanager"),
                "grafana_source_app_host": "http://alertmanager.cos:9093",
            }
        )
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.side_effect = [
                (200, {"database": "ok"}),
                (200, {"uid": "canary-uid-123"}),
                ConnectionRefusedError("connection refused"),
                (200, {"message": "Datasource deleted"}),
            ]

            # WHEN deep validation runs.
            result = validator.validate(level="deep")

        # THEN the transport error is reported and the canary is removed.
        assert result.status == "FAIL"
        health_check = next(c for c in result.checks if c.name == "datasource_health")
        assert "connection refused" in health_check.message
        assert next(c for c in result.checks if c.name == "cleanup_datasource").passed

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

    def test_deep_fails_when_registration_is_not_a_json_object(self) -> None:
        # GIVEN a non-object response from Grafana's registration API.
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.side_effect = [(200, {"database": "ok"}), (200, [])]

            # WHEN deep validation runs.
            result = validator.validate(level="deep")

        # THEN registration fails without attempting a health check.
        assert result.status == "FAIL"
        assert not next(c for c in result.checks if c.name == "register_datasource").passed
        assert not any(c.name == "datasource_health" for c in result.checks)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"status": "ERROR", "message": "datasource unreachable"}, id="datasource-error"),
            pytest.param([], id="json-array"),
        ],
    )
    def test_deep_still_cleans_up_when_datasource_health_check_fails(self, payload: object) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.grafana_datasource.validator._grafana_request") as mock_request:
            mock_request.side_effect = [
                (200, {"database": "ok"}),
                (200, {"uid": "canary-uid-123"}),
                (200, payload),
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
