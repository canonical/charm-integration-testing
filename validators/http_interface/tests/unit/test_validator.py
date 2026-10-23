# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import socket
from typing import cast
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import ops
import pytest

from validators.http_interface.validator import HttpInterfaceValidator
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

VALID_UNIT_DATABAG: dict[str, str] = {
    "hostname": "10.1.2.3",
    "port": "80",
}


def _make_relation(
    unit_databags: list[dict[str, str]],
    endpoint: str = "http-backend",
    app: ApplicationStub | None = None,
) -> RelationStub:
    if app is None:
        app = ApplicationStub()
    units = [UnitStub(name=f"provider/{i}") for i in range(len(unit_databags))]
    data: dict[ApplicationStub | UnitStub | None, dict[str, str]] = {app: {}}
    for unit, databag in zip(units, unit_databags):
        data[unit] = databag
    return RelationStub(name=endpoint, id=0, app=app, data=data, units=frozenset(units))


def _make_validator(
    unit_databags: list[dict[str, str]],
    endpoint: str = "http-backend",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> HttpInterfaceValidator:
    relation = _make_relation(unit_databags, endpoint=endpoint)
    charm = make_charm_from_relation(relation, role=role, interface_name="http")
    return HttpInterfaceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def _mock_http_response(status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.status = status
    resp.read.return_value = b"OK"
    return resp


# ---------------------------------------------------------------------------
# L1 — simple level
# ---------------------------------------------------------------------------


class TestHttpValidatorSimple:
    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.requires, False),
            (RelationRoleStub.provides, True),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_skipped_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN
        validator = _make_validator([VALID_UNIT_DATABAG], role=role)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

    def test_skipped_for_unsupported_level(self) -> None:
        # GIVEN
        validator = _make_validator([VALID_UNIT_DATABAG])

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_error_when_relation_app_is_none(self) -> None:
        # GIVEN a relation with no remote application
        relation = RelationStub(name="http-backend", id=0, app=None)
        charm = make_charm_from_relation(
            RelationStub(name="http-backend", id=0, app=ApplicationStub()),
            role=RelationRoleStub.requires,
            interface_name="http",
        )
        validator = HttpInterfaceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"

    def test_fails_schema_when_no_units(self) -> None:
        # GIVEN a relation with no units at all
        app = ApplicationStub()
        relation = RelationStub(name="http-backend", id=0, app=app, data={app: {}}, units=frozenset())
        charm = make_charm_from_relation(relation, role=RelationRoleStub.requires, interface_name="http")
        validator = HttpInterfaceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "No endpoint data" in schema.message

    def test_fails_schema_when_hostname_missing(self) -> None:
        # GIVEN a unit databag missing 'hostname'
        validator = _make_validator([{"port": "80"}])

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "hostname" in schema.message

    def test_fails_schema_when_port_missing(self) -> None:
        # GIVEN a unit databag missing 'port'
        validator = _make_validator([{"hostname": "10.1.2.3"}])

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "port" in schema.message

    def test_fails_schema_when_port_is_not_integer(self) -> None:
        # GIVEN a unit with non-numeric port
        validator = _make_validator([{"hostname": "10.1.2.3", "port": "not-a-port"}])

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "not a valid integer" in schema.message

    def test_fails_schema_when_port_out_of_range(self) -> None:
        # GIVEN a unit with port 0 (out of valid range)
        validator = _make_validator([{"hostname": "10.1.2.3", "port": "0"}])

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "out of valid range" in schema.message

    def test_passes_schema_with_valid_hostname_and_port(self) -> None:
        # GIVEN valid databag and reachable TCP endpoint
        validator = _make_validator([VALID_UNIT_DATABAG])

        with patch("validators.http_interface.validator._tcp_ping"):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema = next(c for c in result.checks if c.name == "schema")
        assert schema.passed

    def test_fails_connect_when_tcp_unreachable(self) -> None:
        # GIVEN a valid databag but TCP connection is refused
        validator = _make_validator([VALID_UNIT_DATABAG])

        with patch(
            "validators.http_interface.validator._tcp_ping",
            side_effect=socket.timeout("timed out"),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect = next(c for c in result.checks if c.name == "connect")
        assert not connect.passed
        assert "timed out" in connect.message

    def test_passes_with_multiple_units(self) -> None:
        # GIVEN two provider units with distinct hostname:port
        databags = [
            {"hostname": "10.1.2.3", "port": "80"},
            {"hostname": "10.1.2.4", "port": "80"},
        ]
        validator = _make_validator(databags)

        with patch("validators.http_interface.validator._tcp_ping"):
            result = validator.validate(level="simple")

        assert result.status == "PASS"
        schema = next(c for c in result.checks if c.name == "schema")
        assert "2 endpoint(s)" in schema.message

    def test_deduplicates_identical_endpoints(self) -> None:
        # GIVEN two units reporting the same hostname:port (scaled-out provider)
        validator = _make_validator([VALID_UNIT_DATABAG, VALID_UNIT_DATABAG])

        with patch("validators.http_interface.validator._tcp_ping") as mock_ping:
            result = validator.validate(level="simple")

        # Only one TCP ping should be issued for the deduplicated endpoint
        assert mock_ping.call_count == 1
        assert result.status == "PASS"

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator([VALID_UNIT_DATABAG], endpoint="my-http")

        with patch("validators.http_interface.validator._tcp_ping"):
            result = validator.validate(level="simple")

        assert result.endpoint == "my-http"
        assert result.interface == "http"

    def test_connect_error_reported_with_host_and_port(self) -> None:
        # GIVEN a valid databag but a TCP connection failure
        validator = _make_validator([{"hostname": "192.0.2.1", "port": "9999"}])

        with patch(
            "validators.http_interface.validator._tcp_ping",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            result = validator.validate(level="simple")

        assert result.status == "FAIL"
        connect = next(c for c in result.checks if c.name == "connect")
        assert "192.0.2.1:9999" in connect.message


# ---------------------------------------------------------------------------
# L2 — deep level
# ---------------------------------------------------------------------------


class TestHttpValidatorDeep:
    def test_passes_on_successful_http_get(self) -> None:
        # GIVEN a reachable endpoint that returns HTTP 200
        validator = _make_validator([VALID_UNIT_DATABAG])

        with (
            patch("validators.http_interface.validator._tcp_ping"),
            patch("validators.http_interface.validator.urlopen", return_value=_mock_http_response(200)),
        ):
            result = validator.validate(level="deep")

        assert result.status == "PASS"
        probe = next(c for c in result.checks if c.name.startswith("http_probe"))
        assert probe.passed
        assert "200" in probe.message

    def test_passes_when_server_returns_4xx(self) -> None:
        # GIVEN an endpoint returning 404 — still proves HTTP is running
        validator = _make_validator([VALID_UNIT_DATABAG])

        with (
            patch("validators.http_interface.validator._tcp_ping"),
            patch(
                "validators.http_interface.validator.urlopen",
                side_effect=HTTPError("http://10.1.2.3:80/", 404, "Not Found", {}, None),  # type: ignore[arg-type]
            ),
        ):
            result = validator.validate(level="deep")

        assert result.status == "PASS"
        probe = next(c for c in result.checks if c.name.startswith("http_probe"))
        assert probe.passed
        assert "404" in probe.message

    def test_fails_when_http_connection_refused(self) -> None:
        # GIVEN a TCP-reachable host but HTTP connection is refused at the application layer
        validator = _make_validator([VALID_UNIT_DATABAG])

        with (
            patch("validators.http_interface.validator._tcp_ping"),
            patch(
                "validators.http_interface.validator.urlopen",
                side_effect=URLError("Connection refused"),
            ),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        probe = next(c for c in result.checks if c.name.startswith("http_probe"))
        assert not probe.passed
        assert "Connection refused" in probe.message

    def test_deep_still_runs_schema_and_connect_first(self) -> None:
        # GIVEN a missing hostname — should fail at schema before reaching http_probe
        validator = _make_validator([{"port": "80"}])

        result = validator.validate(level="deep")

        assert result.status == "FAIL"
        assert not any(c.name.startswith("http_probe") for c in result.checks)

    def test_check_name_includes_host_and_port(self) -> None:
        # GIVEN
        validator = _make_validator([{"hostname": "192.0.2.5", "port": "8080"}])

        with (
            patch("validators.http_interface.validator._tcp_ping"),
            patch("validators.http_interface.validator.urlopen", return_value=_mock_http_response(200)),
        ):
            result = validator.validate(level="deep")

        probe = next(c for c in result.checks if c.name.startswith("http_probe"))
        assert "192.0.2.5:8080" in probe.name

    def test_deep_skipped_for_uat_level(self) -> None:
        # GIVEN
        validator = _make_validator([VALID_UNIT_DATABAG])

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None
