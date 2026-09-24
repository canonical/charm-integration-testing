# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from importlib.metadata import entry_points
from typing import cast
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

import ops
import yaml

from validators.ingress_per_unit.validator import (
    IngressPerUnitValidator,
    _decode_provider_urls,
    _host_format_check,
    _NoRedirectHandler,
    _port_range_check,
    _unit_url_check,
    _url_format_check,
)
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
    UnitStub,
)

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_INTERFACE = "ingress_per_unit"
_ENDPOINT = "ingress"
_UNIT_NAME = "app/0"

VALID_UNIT_DATA: dict[str, str] = {
    "name": _UNIT_NAME,
    "host": "app-0.app-endpoints.test-model.svc.cluster.local",
    "port": "9090",
    "model": "test-model",
}

VALID_PROVIDER_URL = "http://10.9.43.201/test-model-app-0"


def _provider_databag(urls: dict[str, str | dict[str, str]] | None = None, key: str = "ingress") -> dict[str, str]:
    mapping = {_UNIT_NAME: {"url": VALID_PROVIDER_URL}} if urls is None else urls
    return {key: yaml.safe_dump(mapping)}


def _make_validator(
    unit_databag: dict[str, str],
    provider_databag: dict[str, str] | None = None,
    endpoint: str = _ENDPOINT,
    role: RelationRoleStub = RelationRoleStub.requires,
    remote_app: bool = True,
) -> IngressPerUnitValidator:
    app = ApplicationStub() if remote_app else None
    data: dict[ApplicationStub | UnitStub | None, dict[str, str]] = {}
    if app is not None:
        data[app] = provider_databag or {}
    relation = RelationStub(name=endpoint, id=0, app=app, data=data)
    charm = make_charm_from_relation(relation, role=role, interface_name=_INTERFACE)
    relation.data[charm.unit] = unit_databag
    return IngressPerUnitValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def _mock_response(status: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# Unit-helper tests
# ---------------------------------------------------------------------------


class TestHostFormatCheck:
    def test_valid_hostname(self) -> None:
        assert _host_format_check("app-0.svc.cluster.local").passed

    def test_empty_host(self) -> None:
        assert not _host_format_check("").passed

    def test_url_is_rejected(self) -> None:
        check = _host_format_check("http://app-0.svc")
        assert not check.passed
        assert "bare hostname" in check.message

    def test_whitespace_is_rejected(self) -> None:
        assert not _host_format_check("app 0.svc").passed

    def test_port_is_rejected(self) -> None:
        assert not _host_format_check("app.svc:8080").passed

    def test_delimiters_are_rejected(self) -> None:
        assert not _host_format_check("/tmp").passed
        assert not _host_format_check("user@app.svc").passed


class TestPortRangeCheck:
    def test_valid_port(self) -> None:
        assert _port_range_check("9090").passed

    def test_non_integer(self) -> None:
        check = _port_range_check("http")
        assert not check.passed
        assert "integer" in check.message

    def test_out_of_range(self) -> None:
        assert not _port_range_check("70000").passed
        assert not _port_range_check("0").passed


class TestDecodeProviderUrls:
    def test_ingress_key_with_nested_url(self) -> None:
        check, urls = _decode_provider_urls(_provider_databag())
        assert check.passed
        assert urls == {_UNIT_NAME: VALID_PROVIDER_URL}

    def test_urls_key_with_flat_value(self) -> None:
        databag = {"urls": yaml.safe_dump({_UNIT_NAME: VALID_PROVIDER_URL})}
        check, urls = _decode_provider_urls(databag)
        assert check.passed
        assert urls == {_UNIT_NAME: VALID_PROVIDER_URL}

    def test_missing_key(self) -> None:
        check, urls = _decode_provider_urls({})
        assert not check.passed
        assert urls == {}

    def test_invalid_yaml(self) -> None:
        check, _ = _decode_provider_urls({"ingress": "a: [unclosed"})
        assert not check.passed
        assert "YAML" in check.message

    def test_non_mapping(self) -> None:
        check, _ = _decode_provider_urls({"ingress": yaml.safe_dump(["a", "b"])})
        assert not check.passed
        assert "mapping" in check.message

    def test_no_usable_entries(self) -> None:
        check, _ = _decode_provider_urls({"ingress": yaml.safe_dump({_UNIT_NAME: {"other": "x"}})})
        assert not check.passed

    def test_invalid_yaml_does_not_echo_source(self) -> None:
        check, _ = _decode_provider_urls({"ingress": "unit/0: [unclosed-secret-token"})
        assert not check.passed
        assert "secret-token" not in check.message


class TestUnitUrlCheck:
    def test_unit_present(self) -> None:
        check, url = _unit_url_check({_UNIT_NAME: VALID_PROVIDER_URL}, _UNIT_NAME)
        assert check.passed
        assert url == VALID_PROVIDER_URL

    def test_unit_absent(self) -> None:
        check, url = _unit_url_check({"other/0": VALID_PROVIDER_URL}, _UNIT_NAME)
        assert not check.passed
        assert url == ""
        assert _UNIT_NAME in check.message


class TestUrlFormatCheck:
    def test_valid_http(self) -> None:
        assert _url_format_check("http://10.9.43.201/path").passed

    def test_invalid_scheme(self) -> None:
        assert not _url_format_check("ftp://example.com").passed

    def test_missing_host(self) -> None:
        assert not _url_format_check("http:///path").passed

    def test_missing_host_does_not_echo_userinfo(self) -> None:
        check = _url_format_check("http://secret:password@")
        assert not check.passed
        assert "secret" not in check.message
        assert "password" not in check.message

    def test_success_message_does_not_echo_query_string(self) -> None:
        check = _url_format_check("http://gateway/route?token=super-secret")
        assert check.passed
        assert "token" not in check.message
        assert "super-secret" not in check.message

    def test_success_message_does_not_echo_path(self) -> None:
        check = _url_format_check("http://gateway/secret-token")
        assert check.passed
        assert "secret-token" not in check.message
        assert "gateway" in check.message

    def test_rejects_empty_explicit_port(self) -> None:
        assert not _url_format_check("http://gateway:").passed

    def test_invalid_port_does_not_echo_raw_value(self) -> None:
        check = _url_format_check("http://gateway:secret-token")
        assert not check.passed
        assert "secret-token" not in check.message

    def test_rejects_invalid_hostname_without_echoing_authority(self) -> None:
        check = _url_format_check("http://gateway;token=secret")
        assert not check.passed
        assert "token" not in check.message
        assert "secret" not in check.message

    def test_accepts_ipv6_literal(self) -> None:
        assert _url_format_check("http://[2001:db8::1]/route").passed


# ---------------------------------------------------------------------------
# L1 – simple validation
# ---------------------------------------------------------------------------


class TestIngressPerUnitValidatorSimple:
    def test_skipped_for_unsupported_level(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA)
        result = validator.validate(level="uat")
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_skipped_for_provider_role(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA, role=RelationRoleStub.provides)
        result = validator.validate(level="simple")
        assert result.status == "SKIPPED"
        assert "provides" in (result.error or "")

    def test_error_when_no_remote_app(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA, remote_app=False)
        result = validator.validate(level="simple")
        assert result.status == "ERROR"

    def test_pass_with_valid_unit_data(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA)
        result = validator.validate(level="simple")
        assert result.status == "PASS", result.checks

    def test_fail_missing_fields(self) -> None:
        validator = _make_validator({"name": _UNIT_NAME})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "host" in schema_check.message
        assert "port" in schema_check.message
        assert "model" in schema_check.message

    def test_fail_empty_unit_databag(self) -> None:
        validator = _make_validator({})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        assert any(c.name == "schema" and not c.passed for c in result.checks)

    def test_fail_invalid_host(self) -> None:
        validator = _make_validator({**VALID_UNIT_DATA, "host": "http://bad"})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        assert any(c.name == "host_format" and not c.passed for c in result.checks)

    def test_fail_invalid_port(self) -> None:
        validator = _make_validator({**VALID_UNIT_DATA, "port": "not-a-port"})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        assert any(c.name == "port_range" and not c.passed for c in result.checks)


# ---------------------------------------------------------------------------
# L2 – deep validation
# ---------------------------------------------------------------------------


class TestIngressPerUnitValidatorDeep:
    def test_opener_disables_environment_proxies(self) -> None:
        with patch("urllib.request.getproxies", return_value={"http": "http://proxy.example:3128"}) as getproxies:
            opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
        proxy_handlers = [handler for handler in getattr(opener, "handlers") if isinstance(handler, ProxyHandler)]
        assert not proxy_handlers
        getproxies.assert_not_called()

    def test_pass_when_url_reachable(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA, _provider_databag())
        with (
            patch("validators.ingress_per_unit.validator.socket.create_connection"),
            patch("validators.ingress_per_unit.validator._HTTP_OPENER.open", return_value=_mock_response()),
        ):
            result = validator.validate(level="deep")
        assert result.status == "PASS", result.checks

    def test_pass_when_url_redirects_without_following(self) -> None:
        response = HTTPError(VALID_PROVIDER_URL, 302, "Found", {}, None)  # type: ignore[arg-type]
        validator = _make_validator(VALID_UNIT_DATA, _provider_databag())
        with (
            patch("validators.ingress_per_unit.validator.socket.create_connection"),
            patch("validators.ingress_per_unit.validator._HTTP_OPENER.open", side_effect=response) as open_request,
        ):
            result = validator.validate(level="deep")
        probe_check = next(check for check in result.checks if check.name == "http_probe")
        assert probe_check.passed
        assert "302" in probe_check.message
        open_request.assert_called_once()

    def test_closes_http_error_response(self) -> None:
        response = HTTPError(VALID_PROVIDER_URL, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        response.close = MagicMock()
        validator = _make_validator(VALID_UNIT_DATA, _provider_databag())
        with (
            patch("validators.ingress_per_unit.validator.socket.create_connection"),
            patch("validators.ingress_per_unit.validator._HTTP_OPENER.open", side_effect=response),
        ):
            result = validator.validate(level="deep")
        assert result.status == "PASS"
        response.close.assert_called_once()

    def test_fail_when_http_transport_fails(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA, _provider_databag())
        with (
            patch("validators.ingress_per_unit.validator.socket.create_connection"),
            patch(
                "validators.ingress_per_unit.validator._HTTP_OPENER.open",
                side_effect=URLError("connection refused"),
            ),
        ):
            result = validator.validate(level="deep")
        probe_check = next(check for check in result.checks if check.name == "http_probe")
        assert not probe_check.passed
        assert "connection refused" in probe_check.message

    def test_fail_when_provider_has_no_urls(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA, {})
        result = validator.validate(level="deep")
        assert result.status == "FAIL"
        assert any(c.name == "provider_urls" and not c.passed for c in result.checks)

    def test_fail_when_unit_not_advertised(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA, _provider_databag({"other/0": {"url": VALID_PROVIDER_URL}}))
        result = validator.validate(level="deep")
        assert result.status == "FAIL"
        assert any(c.name == "unit_url" and not c.passed for c in result.checks)

    def test_fail_when_url_malformed(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA, _provider_databag({_UNIT_NAME: {"url": "http://[::1"}}))
        result = validator.validate(level="deep")
        assert result.status == "FAIL"
        assert any(c.name == "url_format" and not c.passed for c in result.checks)

    def test_fail_when_unreachable(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA, _provider_databag())
        with patch(
            "validators.ingress_per_unit.validator.socket.create_connection",
            side_effect=OSError("connection refused"),
        ):
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "connection refused" in connect_check.message

    def test_fail_missing_required_fields(self) -> None:
        validator = _make_validator({"name": _UNIT_NAME}, _provider_databag())
        result = validator.validate(level="deep")
        assert result.status == "FAIL"
        assert any(c.name == "schema" and not c.passed for c in result.checks)

    def test_error_when_no_remote_app(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA, remote_app=False)
        result = validator.validate(level="deep")
        assert result.status == "ERROR"

    def test_skipped_for_provider_role(self) -> None:
        validator = _make_validator(VALID_UNIT_DATA, role=RelationRoleStub.provides)
        result = validator.validate(level="deep")
        assert result.status == "SKIPPED"


# ---------------------------------------------------------------------------
# Packaging / entry point
# ---------------------------------------------------------------------------


class TestEntryPoint:
    """Guard the packaging contract the runner relies on.

    The runner discovers validators through the ``endpoint_validators`` entry
    point and calls ``ep.load()``, which imports ``validators.ingress_per_unit``
    and looks the class up on that module. An empty package ``__init__`` therefore
    silently produces zero results on a live unit, so assert the export exists.
    """

    def test_entry_point_is_declared_for_interface(self) -> None:
        eps = [ep for ep in entry_points(group="endpoint_validators") if ep.name == _INTERFACE]
        assert eps, f"No '{_INTERFACE}' entry point found in group 'endpoint_validators'."

    def test_entry_point_loads_validator_class(self) -> None:
        ep = next(ep for ep in entry_points(group="endpoint_validators") if ep.name == _INTERFACE)
        assert ep.load() is IngressPerUnitValidator
