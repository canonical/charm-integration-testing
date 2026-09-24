# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import urllib.error
import urllib.request
from importlib.metadata import entry_points
from typing import cast
from unittest.mock import MagicMock, patch

import ops

from validators.catalogue.validator import (
    CatalogueValidator,
    _validate_api_endpoints,
    _validate_item_served,
    _validate_url_syntax,
)
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_INTERFACE = "catalogue"
_ENDPOINT = "catalogue"


def _make_validator(
    local_databag: dict[str, str],
    endpoint: str = _ENDPOINT,
    role: RelationRoleStub = RelationRoleStub.requires,
) -> CatalogueValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: {}})
    stub_charm = make_charm_from_relation(relation, interface_name=_INTERFACE, role=role)
    relation.data[stub_charm.app] = local_databag
    return CatalogueValidator(cast(ops.CharmBase, stub_charm), cast(ops.Relation, relation))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "name": "Alertmanager",
    "url": "http://alertmanager-k8s-0.alertmanager-k8s-endpoints.test-model.svc.cluster.local:9093",
    "icon": "bell-alert",
    "description": "Alertmanager handles alerts.",
    "api_docs": "https://example.com/openapi.yaml",
    "api_endpoints": json.dumps({"Alerts": "http://alertmanager:9093/api/v2/alerts"}),
}

VALID_PAYLOAD: dict[str, object] = {
    "title": "Service Catalogue",
    "apps": [
        {
            "name": "Alertmanager",
            "url": VALID_DATABAG["url"],
            "icon": "bell-alert",
        }
    ],
}


def _mock_response(payload: object, status: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# Unit-helper tests
# ---------------------------------------------------------------------------


class TestValidateUrlSyntax:
    def test_valid_http_url(self) -> None:
        assert _validate_url_syntax("http://example.com:9093").passed

    def test_valid_https_url(self) -> None:
        assert _validate_url_syntax("https://catalogue.internal:8443").passed

    def test_invalid_scheme(self) -> None:
        check = _validate_url_syntax("ftp://example.com")
        assert not check.passed
        assert "http" in check.message

    def test_invalid_hostname_or_port(self) -> None:
        assert not _validate_url_syntax("http://:9093").passed
        assert not _validate_url_syntax("http://example.com:abc").passed
        assert not _validate_url_syntax("http://example.com/a b").passed

    def test_missing_host(self) -> None:
        assert not _validate_url_syntax("http:///path/only").passed


class TestValidateApiEndpoints:
    def test_empty_is_accepted(self) -> None:
        assert _validate_api_endpoints("").passed
        assert _validate_api_endpoints("null").passed

    def test_valid_json_object(self) -> None:
        assert _validate_api_endpoints('{"Alerts": "http://x/api"}').passed

    def test_invalid_json_fails(self) -> None:
        check = _validate_api_endpoints("not-json")
        assert not check.passed
        assert "JSON" in check.message

    def test_non_object_fails(self) -> None:
        check = _validate_api_endpoints('["a", "b"]')
        assert not check.passed
        assert "object" in check.message


class TestValidateItemServed:
    def test_item_present(self) -> None:
        assert _validate_item_served(VALID_PAYLOAD, VALID_DATABAG).passed

    def test_item_absent(self) -> None:
        check = _validate_item_served(VALID_PAYLOAD, {**VALID_DATABAG, "name": "Prometheus"})
        assert not check.passed
        assert "Prometheus" in check.message

    def test_no_apps_list(self) -> None:
        check = _validate_item_served({"title": "x"}, VALID_DATABAG)
        assert not check.passed
        assert "apps" in check.message

    def test_none_payload(self) -> None:
        assert not _validate_item_served(None, VALID_DATABAG).passed

    def test_required_field_missing(self) -> None:
        payload = {"apps": [{"name": VALID_DATABAG["name"], "icon": VALID_DATABAG["icon"]}]}
        assert not _validate_item_served(payload, VALID_DATABAG).passed

    def test_provider_hostname_override_is_accepted(self) -> None:
        payload = {
            "apps": [
                {
                    **VALID_PAYLOAD["apps"][0],
                    "url": "http://public.example.test:9093",
                }
            ]
        }
        assert _validate_item_served(payload, VALID_DATABAG).passed


# ---------------------------------------------------------------------------
# L1 – simple validation
# ---------------------------------------------------------------------------


class TestCatalogueValidatorSimple:
    def test_skipped_for_unsupported_level(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        result = validator.validate(level="uat")
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_skipped_for_provider_role(self) -> None:
        validator = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides)
        result = validator.validate(level="simple")
        assert result.status == "SKIPPED"
        assert "provides" in (result.error or "")

    def test_error_when_no_remote_app(self) -> None:
        relation = RelationStub(name=_ENDPOINT, id=0, app=None, data={})
        charm = cast(ops.CharmBase, make_charm_from_relation(relation, interface_name=_INTERFACE))
        validator = CatalogueValidator(charm, cast(ops.Relation, relation))
        result = validator.validate(level="simple")
        assert result.status == "ERROR"

    def test_pass_with_valid_databag(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        result = validator.validate(level="simple")
        assert result.status == "PASS", result.checks

    def test_fail_missing_name(self) -> None:
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "name"}
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "name" in schema_check.message

    def test_fail_missing_url_and_icon(self) -> None:
        validator = _make_validator({"name": "Alertmanager"})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert "url" in schema_check.message
        assert "icon" in schema_check.message

    def test_fail_empty_local_databag(self) -> None:
        validator = _make_validator({})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        assert any(c.name == "schema" and not c.passed for c in result.checks)

    def test_fail_invalid_url_scheme(self) -> None:
        validator = _make_validator({**VALID_DATABAG, "url": "ftp://bad.example.com"})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        url_check = next(c for c in result.checks if c.name == "url_syntax")
        assert not url_check.passed

    def test_fail_invalid_api_endpoints(self) -> None:
        validator = _make_validator({**VALID_DATABAG, "api_endpoints": "not-json"})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        endpoints_check = next(c for c in result.checks if c.name == "api_endpoints")
        assert not endpoints_check.passed

    def test_pass_without_optional_api_endpoints(self) -> None:
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "api_endpoints"}
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "PASS", result.checks


# ---------------------------------------------------------------------------
# L2 – deep validation
# ---------------------------------------------------------------------------


class TestCatalogueValidatorDeep:
    def test_pass_when_item_served(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.catalogue.validator._HTTP_OPENER.open", return_value=_mock_response(VALID_PAYLOAD)):
            result = validator.validate(level="deep")
        assert result.status == "PASS", result.checks

    def test_fail_when_item_not_served(self) -> None:
        payload = {"apps": [{"name": "Prometheus"}]}
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.catalogue.validator._HTTP_OPENER.open", return_value=_mock_response(payload)):
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        item_check = next(c for c in result.checks if c.name == "item_served")
        assert not item_check.passed

    def test_fail_when_provider_unreachable(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch(
            "validators.catalogue.validator._HTTP_OPENER.open", side_effect=urllib.error.URLError("connection refused")
        ):
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        reach_check = next(c for c in result.checks if c.name == "http_reachability")
        assert not reach_check.passed
        assert "connection refused" in reach_check.message

    def test_fail_when_http_error(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch(
            "validators.catalogue.validator._HTTP_OPENER.open",
            side_effect=urllib.error.HTTPError("http://x", 404, "Not Found", {}, None),  # type: ignore[arg-type]
        ):
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        reach_check = next(c for c in result.checks if c.name == "http_reachability")
        assert not reach_check.passed

    def test_fail_when_body_not_json(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"<html>not json</html>"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("validators.catalogue.validator._HTTP_OPENER.open", return_value=mock_resp):
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        reach_check = next(c for c in result.checks if c.name == "http_reachability")
        assert not reach_check.passed

    def test_fail_missing_required_fields(self) -> None:
        validator = _make_validator({"name": "Alertmanager"})
        result = validator.validate(level="deep")
        assert result.status == "FAIL"
        assert any(c.name == "schema" and not c.passed for c in result.checks)

    def test_error_when_no_remote_app(self) -> None:
        relation = RelationStub(name=_ENDPOINT, id=0, app=None, data={})
        charm = cast(ops.CharmBase, make_charm_from_relation(relation, interface_name=_INTERFACE))
        validator = CatalogueValidator(charm, cast(ops.Relation, relation))
        result = validator.validate(level="deep")
        assert result.status == "ERROR"

    def test_skipped_for_provider_role(self) -> None:
        validator = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides)
        result = validator.validate(level="deep")
        assert result.status == "SKIPPED"

    def test_config_url_derived_from_relation_and_model(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        captured: dict[str, urllib.request.Request] = {}

        def fake_urlopen(req: urllib.request.Request, timeout: float | None = None) -> MagicMock:
            captured["req"] = req
            return _mock_response(VALID_PAYLOAD)

        with patch("validators.catalogue.validator._HTTP_OPENER.open", side_effect=fake_urlopen):
            validator.validate(level="deep")

        assert captured["req"].full_url == "http://app.test-model.svc.cluster.local/config.json"


# ---------------------------------------------------------------------------
# Packaging / entry point
# ---------------------------------------------------------------------------


class TestEntryPoint:
    """Guard the packaging contract the runner relies on.

    The runner discovers validators through the ``endpoint_validators`` entry
    point and calls ``ep.load()``, which imports ``validators.catalogue`` and
    looks the class up on that module. An empty package ``__init__`` therefore
    silently produces zero results on a live unit, so assert the export exists.
    """

    def test_entry_point_is_declared_for_catalogue_interface(self) -> None:
        eps = [ep for ep in entry_points(group="endpoint_validators") if ep.name == _INTERFACE]
        assert eps, "No 'catalogue' entry point found in group 'endpoint_validators'."

    def test_entry_point_loads_validator_class(self) -> None:
        ep = next(ep for ep in entry_points(group="endpoint_validators") if ep.name == _INTERFACE)
        assert ep.load() is CatalogueValidator
