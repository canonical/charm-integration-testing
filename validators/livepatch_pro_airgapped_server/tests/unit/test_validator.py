# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the livepatch-pro-airgapped-server validator.

The livepatch-pro-airgapped-server interface uses **unit** databags:
the provider writes ``hostname``, ``scheme``, and ``port`` to each unit's
own unit databag (not the application databag).  The stubs used here mirror
that structure.
"""

import urllib.error
from typing import cast
from unittest.mock import MagicMock, patch

import ops
import pytest

from validators.livepatch_pro_airgapped_server.validator import (
    LivepatchProAirgappedServerValidator,
    _build_url,
    _check_http_canary,
    _check_port,
    _check_scheme,
    _check_tcp,
    _check_units_published,
    _first_populated_unit,
    _unit_databags,
)
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
    UnitStub,
)

# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

_INTERFACE = "livepatch-pro-airgapped-server"
_ENDPOINT = "pro-airgapped-server"


def _make_validator(
    unit_databags: dict[UnitStub, dict[str, str]] | None = None,
    app_databag: dict[str, str] | None = None,
    endpoint: str = _ENDPOINT,
    role: RelationRoleStub = RelationRoleStub.requires,
    app: ApplicationStub | None = None,
) -> LivepatchProAirgappedServerValidator:
    """Create a validator with provider unit databags and optional app databag."""
    if app is None:
        app = ApplicationStub()
    units = frozenset(unit_databags.keys()) if unit_databags else frozenset()
    data: dict[ApplicationStub | UnitStub | None, dict[str, str]] = {app: app_databag or {}}
    if unit_databags:
        for k, v in unit_databags.items():
            data[k] = v
    relation = RelationStub(name=endpoint, id=0, app=app, data=data, units=units)
    charm = cast(ops.CharmBase, make_charm_from_relation(relation, interface_name=_INTERFACE, role=role))
    return LivepatchProAirgappedServerValidator(charm, cast(ops.Relation, relation))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

VALID_UNIT_DATABAG: dict[str, str] = {
    "hostname": "10.0.0.5",
    "scheme": "http",
    "port": "8484",
}


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestCheckUnitsPublished:
    def test_passes_when_hostname_present(self) -> None:
        check = _check_units_published([{"hostname": "myhost", "scheme": "http"}])
        assert check.passed
        assert "myhost" in check.message

    def test_fails_when_no_unit_data(self) -> None:
        check = _check_units_published([])
        assert not check.passed
        assert "No unit data" in check.message

    def test_fails_when_hostname_missing(self) -> None:
        check = _check_units_published([{"scheme": "http", "port": "8484"}])
        assert not check.passed
        assert "hostname" in check.message

    def test_passes_for_first_unit_with_hostname(self) -> None:
        # Second unit has hostname; first does not.
        dbs = [{"scheme": "http"}, {"hostname": "host2", "port": "80"}]
        check = _check_units_published(dbs)
        assert check.passed
        assert "host2" in check.message


class TestCheckScheme:
    def test_passes_for_http(self) -> None:
        assert _check_scheme("http").passed

    def test_passes_for_https(self) -> None:
        assert _check_scheme("https").passed

    def test_fails_for_unknown_scheme(self) -> None:
        check = _check_scheme("ftp")
        assert not check.passed
        assert "ftp" in check.message
        assert "http" in check.message

    def test_fails_for_empty_scheme(self) -> None:
        check = _check_scheme("")
        assert not check.passed


class TestCheckPort:
    def test_passes_for_valid_port(self) -> None:
        check, port = _check_port("8484")
        assert check.passed
        assert port == 8484

    def test_passes_for_empty_port(self) -> None:
        check, port = _check_port("")
        assert check.passed
        assert port is None

    def test_fails_for_non_integer(self) -> None:
        check, port = _check_port("not-a-port")
        assert not check.passed
        assert port is None
        assert "not-a-port" in check.message

    def test_fails_for_port_zero(self) -> None:
        check, port = _check_port("0")
        assert not check.passed
        assert port is None

    def test_fails_for_port_above_65535(self) -> None:
        check, port = _check_port("99999")
        assert not check.passed
        assert port is None

    def test_passes_for_port_1(self) -> None:
        check, port = _check_port("1")
        assert check.passed
        assert port == 1

    def test_passes_for_port_65535(self) -> None:
        check, port = _check_port("65535")
        assert check.passed
        assert port == 65535


class TestBuildUrl:
    def test_with_port(self) -> None:
        assert _build_url("http", "myhost", 8484) == "http://myhost:8484"

    def test_without_port(self) -> None:
        assert _build_url("https", "myhost", None) == "https://myhost"

    def test_ipv6_literal_without_port(self) -> None:
        assert _build_url("http", "2001:db8::1", None) == "http://[2001:db8::1]"

    def test_ipv6_literal_with_port(self) -> None:
        assert _build_url("https", "2001:db8::1", 8484) == "https://[2001:db8::1]:8484"

    def test_ipv6_literal_already_bracketed(self) -> None:
        assert _build_url("http", "[2001:db8::1]", None) == "http://[2001:db8::1]"


class TestCheckTcp:
    def test_default_port_for_http_is_80(self) -> None:
        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
        ) as mock_conn:
            check = _check_tcp("myhost", None, "http")
        assert check.passed
        assert mock_conn.call_args[0][0] == ("myhost", 80)

    def test_default_port_for_https_is_443(self) -> None:
        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
        ) as mock_conn:
            check = _check_tcp("myhost", None, "https")
        assert check.passed
        assert mock_conn.call_args[0][0] == ("myhost", 443)

    def test_explicit_port_overrides_scheme_default(self) -> None:
        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
        ) as mock_conn:
            check = _check_tcp("myhost", 8484, "https")
        assert check.passed
        assert mock_conn.call_args[0][0] == ("myhost", 8484)


class TestFirstPopulatedUnit:
    def test_returns_first_with_hostname(self) -> None:
        dbs = [{"scheme": "http"}, {"hostname": "found", "port": "8484"}]
        result = _first_populated_unit(dbs)
        assert result["hostname"] == "found"

    def test_returns_empty_dict_when_none_have_hostname(self) -> None:
        dbs = [{"scheme": "http"}, {"port": "80"}]
        result = _first_populated_unit(dbs)
        assert result == {}

    def test_returns_empty_dict_for_empty_list(self) -> None:
        assert _first_populated_unit([]) == {}


class TestUnitDatabags:
    def test_returns_unit_databags(self) -> None:
        unit1 = UnitStub("provider/0")
        unit2 = UnitStub("provider/1")
        app = ApplicationStub()
        relation = RelationStub(
            name="rel",
            id=0,
            app=app,
            data={
                app: {},
                unit1: {"hostname": "host1"},
                unit2: {"hostname": "host2"},
            },
            units=frozenset([unit1, unit2]),
        )
        dbs = _unit_databags(cast(ops.Relation, relation))
        assert len(dbs) == 2
        hostnames = {db["hostname"] for db in dbs}
        assert hostnames == {"host1", "host2"}

    def test_skips_units_with_no_data_entry(self) -> None:
        unit_with_data = UnitStub("provider/0")
        unit_no_data = UnitStub("provider/1")
        app = ApplicationStub()
        relation = RelationStub(
            name="rel",
            id=0,
            app=app,
            data={app: {}, unit_with_data: {"hostname": "host1"}},
            units=frozenset([unit_with_data, unit_no_data]),
        )
        dbs = _unit_databags(cast(ops.Relation, relation))
        assert len(dbs) == 1
        assert dbs[0]["hostname"] == "host1"


# ---------------------------------------------------------------------------
# L1 (simple) tests
# ---------------------------------------------------------------------------


class TestSimpleValidation:
    def test_returns_error_when_no_remote_app(self) -> None:
        # GIVEN a relation with no remote app (app=None)
        relation = RelationStub(name=_ENDPOINT, id=0, app=None, data={})
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name=_INTERFACE, role=RelationRoleStub.requires),
        )
        validator = LivepatchProAirgappedServerValidator(charm, cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"
        assert result.error is not None

    def test_fails_when_no_unit_data(self) -> None:
        # GIVEN a relation with no unit databags
        validator = _make_validator(unit_databags=None)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        unit_check = next(c for c in result.checks if c.name == "unit_data")
        assert not unit_check.passed

    def test_fails_when_hostname_missing_in_all_units(self) -> None:
        # GIVEN a provider unit that published no hostname
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: {"scheme": "http", "port": "8484"}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        unit_check = next(c for c in result.checks if c.name == "unit_data")
        assert not unit_check.passed
        assert "hostname" in unit_check.message

    def test_fails_when_scheme_is_invalid(self) -> None:
        # GIVEN a provider unit with an unsupported scheme
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: {"hostname": "host", "scheme": "ftp", "port": "21"}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        scheme_check = next(c for c in result.checks if c.name == "scheme")
        assert not scheme_check.passed
        assert "ftp" in scheme_check.message

    def test_fails_when_port_is_invalid(self) -> None:
        # GIVEN a provider unit with a non-numeric port
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: {"hostname": "host", "scheme": "http", "port": "NaN"}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        port_check = next(c for c in result.checks if c.name == "port")
        assert not port_check.passed

    def test_fails_when_tcp_unreachable(self) -> None:
        # GIVEN a valid databag but TCP connection refused
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: dict(VALID_UNIT_DATABAG)})

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
            side_effect=OSError("Connection refused"),
        ):
            result = validator.validate(level="simple")

        assert result.status == "FAIL"
        tcp_check = next(c for c in result.checks if c.name == "tcp_connect")
        assert not tcp_check.passed
        assert "Connection refused" in tcp_check.message

    def test_passes_with_valid_fields_and_tcp(self) -> None:
        # GIVEN a valid databag and a successful TCP connection
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: dict(VALID_UNIT_DATABAG)})

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
            return_value=mock_conn,
        ):
            result = validator.validate(level="simple")

        assert result.status == "PASS"
        assert all(c.passed for c in result.checks)

    def test_passes_when_scheme_defaults_to_http_when_absent(self) -> None:
        # GIVEN a provider that did not publish 'scheme' (defaults to http)
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: {"hostname": "10.0.0.5", "port": "8080"}})

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
            return_value=mock_conn,
        ):
            result = validator.validate(level="simple")

        assert result.status == "PASS"

    def test_passes_when_port_absent(self) -> None:
        # GIVEN a provider that did not publish 'port'
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: {"hostname": "10.0.0.5", "scheme": "http"}})

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
            return_value=mock_conn,
        ):
            result = validator.validate(level="simple")

        assert result.status == "PASS"
        port_check = next(c for c in result.checks if c.name == "port")
        assert port_check.passed
        assert "default" in port_check.message

    def test_skips_for_provides_role(self) -> None:
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: dict(VALID_UNIT_DATABAG)}, role=RelationRoleStub.provides)

        result = validator.validate(level="simple")

        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_skips_for_peer_role(self) -> None:
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: dict(VALID_UNIT_DATABAG)}, role=RelationRoleStub.peer)

        result = validator.validate(level="simple")

        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_skips_for_uat_level(self) -> None:
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: dict(VALID_UNIT_DATABAG)})

        result = validator.validate(level="uat")

        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_result_reflects_endpoint_and_interface(self) -> None:
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: dict(VALID_UNIT_DATABAG)}, endpoint="my-endpoint")

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
            return_value=mock_conn,
        ):
            result = validator.validate(level="simple")

        assert result.endpoint == "my-endpoint"
        assert result.interface == _INTERFACE


# ---------------------------------------------------------------------------
# L2 (deep) tests
# ---------------------------------------------------------------------------


class TestDeepValidation:
    def test_returns_error_when_no_remote_app(self) -> None:
        relation = RelationStub(name=_ENDPOINT, id=0, app=None, data={})
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name=_INTERFACE, role=RelationRoleStub.requires),
        )
        validator = LivepatchProAirgappedServerValidator(charm, cast(ops.Relation, relation))

        result = validator.validate(level="deep")

        assert result.status == "ERROR"

    def test_fails_schema_when_hostname_missing(self) -> None:
        # GIVEN no unit published hostname
        validator = _make_validator(unit_databags=None)

        result = validator.validate(level="deep")

        assert result.status == "FAIL"
        unit_check = next(c for c in result.checks if c.name == "unit_data")
        assert not unit_check.passed

    def test_fails_when_tcp_unreachable(self) -> None:
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: dict(VALID_UNIT_DATABAG)})

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
            side_effect=OSError("Network unreachable"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        tcp_check = next(c for c in result.checks if c.name == "tcp_connect")
        assert not tcp_check.passed
        assert not any(c.name == "http_canary" for c in result.checks)

    def test_fails_http_canary_on_server_error(self) -> None:
        # GIVEN TCP succeeds but the server returns HTTP 500
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: dict(VALID_UNIT_DATABAG)})

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
            return_value=mock_conn,
        ):
            with patch(
                "validators.livepatch_pro_airgapped_server.validator.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(url="", code=500, msg="Internal Server Error", hdrs=None, fp=None),  # type: ignore[arg-type]
            ):
                result = validator.validate(level="deep")

        assert result.status == "FAIL"
        http_check = next(c for c in result.checks if c.name == "http_canary")
        assert not http_check.passed
        assert "500" in http_check.message

    def test_passes_http_canary_on_401_unauthorized(self) -> None:
        # GIVEN TCP succeeds and server returns 401 (auth required — server is up)
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: dict(VALID_UNIT_DATABAG)})

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
            return_value=mock_conn,
        ):
            with patch(
                "validators.livepatch_pro_airgapped_server.validator.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(url="", code=401, msg="Unauthorized", hdrs=None, fp=None),  # type: ignore[arg-type]
            ):
                result = validator.validate(level="deep")

        assert result.status == "PASS"
        http_check = next(c for c in result.checks if c.name == "http_canary")
        assert http_check.passed
        assert "401" in http_check.message

    def test_fails_http_canary_on_url_error(self) -> None:
        # GIVEN TCP succeeds but HTTP request cannot connect
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: dict(VALID_UNIT_DATABAG)})

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
            return_value=mock_conn,
        ):
            with patch(
                "validators.livepatch_pro_airgapped_server.validator.urllib.request.urlopen",
                side_effect=urllib.error.URLError("Connection refused"),
            ):
                result = validator.validate(level="deep")

        assert result.status == "FAIL"
        http_check = next(c for c in result.checks if c.name == "http_canary")
        assert not http_check.passed
        assert "Connection refused" in http_check.message

    def test_passes_with_valid_fields_and_http_200(self) -> None:
        # GIVEN a valid databag, TCP succeeds, and HTTP returns 200
        unit = UnitStub("provider/0")
        validator = _make_validator(unit_databags={unit: dict(VALID_UNIT_DATABAG)})

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers.get.return_value = "application/json"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
            return_value=mock_conn,
        ):
            with patch(
                "validators.livepatch_pro_airgapped_server.validator.urllib.request.urlopen",
                return_value=mock_resp,
            ):
                result = validator.validate(level="deep")

        assert result.status == "PASS"
        assert result.level == "deep"
        http_check = next(c for c in result.checks if c.name == "http_canary")
        assert http_check.passed
        assert "200" in http_check.message

    def test_multiple_units_uses_first_with_hostname(self) -> None:
        # GIVEN two units; first has no hostname, second does
        unit1 = UnitStub("provider/0")
        unit2 = UnitStub("provider/1")
        validator = _make_validator(
            unit_databags={
                unit1: {"scheme": "http"},
                unit2: {"hostname": "10.0.0.9", "scheme": "https", "port": "443"},
            }
        )

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers.get.return_value = "application/json"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.socket.create_connection",
            return_value=mock_conn,
        ) as mock_tcp:
            with patch(
                "validators.livepatch_pro_airgapped_server.validator.urllib.request.urlopen",
                return_value=mock_resp,
            ):
                result = validator.validate(level="deep")

        assert result.status == "PASS"
        # TCP should be called with the second unit's hostname
        call_args = mock_tcp.call_args
        assert call_args[0][0] == ("10.0.0.9", 443)


# ---------------------------------------------------------------------------
# _check_http_canary unit tests
# ---------------------------------------------------------------------------


class TestCheckHttpCanary:
    def test_passes_on_http_200(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers.get.return_value = "application/json"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "validators.livepatch_pro_airgapped_server.validator.urllib.request.urlopen", return_value=mock_resp
        ):
            check = _check_http_canary("http://host:8484")

        assert check.passed
        assert "200" in check.message

    def test_passes_on_http_404(self) -> None:
        with patch(
            "validators.livepatch_pro_airgapped_server.validator.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(url="", code=404, msg="Not Found", hdrs=None, fp=None),  # type: ignore[arg-type]
        ):
            check = _check_http_canary("http://host:8484")

        assert check.passed
        assert "404" in check.message

    def test_fails_on_http_503(self) -> None:
        with patch(
            "validators.livepatch_pro_airgapped_server.validator.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(url="", code=503, msg="Service Unavailable", hdrs=None, fp=None),  # type: ignore[arg-type]
        ):
            check = _check_http_canary("http://host:8484")

        assert not check.passed
        assert "503" in check.message

    def test_fails_on_url_error(self) -> None:
        with patch(
            "validators.livepatch_pro_airgapped_server.validator.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection timed out"),
        ):
            check = _check_http_canary("http://host:8484")

        assert not check.passed
        assert "Connection timed out" in check.message

    @pytest.mark.parametrize("code", [400, 401, 403, 404])
    def test_passes_for_all_client_error_codes(self, code: int) -> None:
        with patch(
            "validators.livepatch_pro_airgapped_server.validator.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(url="", code=code, msg="Error", hdrs=None, fp=None),  # type: ignore[arg-type]
        ):
            check = _check_http_canary("http://host:8484")

        assert check.passed, f"Expected 4xx code {code} to be treated as PASS (server is reachable)"
