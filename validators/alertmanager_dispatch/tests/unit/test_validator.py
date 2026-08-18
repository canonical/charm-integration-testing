# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import urllib.error
from typing import cast
from unittest.mock import MagicMock, patch

import ops

from validators.alertmanager_dispatch.validator import AlertmanagerDispatchValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
    UnitStub,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_URL = "http://alertmanager-k8s-0.alertmanager-k8s-endpoints.am-test.svc.cluster.local:9093"


def _make_validator(
    unit_databags: dict[str, dict[str, str]] | None = None,
    endpoint: str = "alerting",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> AlertmanagerDispatchValidator:
    """Create a validator with the given provider unit databags.

    *unit_databags* maps unit name -> databag dict. Defaults to a single
    Alertmanager unit advertising a valid ``url``.
    """
    if unit_databags is None:
        unit_databags = {"alertmanager-k8s/0": {"url": _URL}}

    app = ApplicationStub()
    units = frozenset(UnitStub(name) for name in unit_databags)
    data: dict[ApplicationStub | UnitStub | None, dict[str, str]] = {app: {}}
    for unit in units:
        data[unit] = unit_databags[unit.name]

    relation = RelationStub(name=endpoint, id=0, app=app, data=data, units=units)
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(relation, role=role, interface_name="alertmanager_dispatch"),
    )
    return AlertmanagerDispatchValidator(charm, cast(ops.Relation, relation))


def _mock_http_response(status: int = 200, body: bytes = b"") -> MagicMock:
    """Return a context-manager mock that yields an HTTP response."""
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.status = status
    resp.read.return_value = body
    return resp


def _mock_http_error(status: int) -> urllib.error.HTTPError:
    """Return an HTTPError for the given status code (used as urlopen side_effect)."""
    return urllib.error.HTTPError(url="", code=status, msg="", hdrs=None, fp=None)  # type: ignore[arg-type]


_ALERTS_FOUND_BODY = json.dumps(
    [
        {
            "labels": {"alertname": "EndpointValidatorCanary", "validator_probe": "abc123def456"},
            "status": {"state": "active"},
        }
    ]
).encode()

_ALERTS_EMPTY_BODY = json.dumps([]).encode()


# ---------------------------------------------------------------------------
# Tests: role and level guards
# ---------------------------------------------------------------------------


class TestAlertmanagerDispatchValidatorGuards:
    def test_returns_skipped_for_uat_level(self) -> None:
        # GIVEN
        validator = _make_validator()

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_returns_skipped_for_provides_role(self) -> None:
        # GIVEN
        validator = _make_validator(role=RelationRoleStub.provides)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "SKIPPED"
        assert result.checks == []
        assert result.error is not None
        assert "provides" in result.error

    def test_returns_skipped_for_peer_role(self) -> None:
        # GIVEN
        validator = _make_validator(role=RelationRoleStub.peer)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "SKIPPED"
        assert result.role == "peer"

    def test_returns_error_when_no_remote_app(self) -> None:
        # GIVEN a relation whose app is None
        app = ApplicationStub()
        relation = RelationStub(name="alerting", id=0, app=app, data={app: {}})
        relation.app = None
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="alertmanager_dispatch"),
        )
        validator = AlertmanagerDispatchValidator(charm, cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"
        assert result.error is not None


# ---------------------------------------------------------------------------
# Tests: schema validation (L1)
# ---------------------------------------------------------------------------


class TestAlertmanagerDispatchValidatorSchema:
    def test_fails_when_no_units(self) -> None:
        # GIVEN an empty units set -> no dispatch data
        app = ApplicationStub()
        relation = RelationStub(name="alerting", id=0, app=app, data={app: {}}, units=frozenset())
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="alertmanager_dispatch"),
        )
        validator = AlertmanagerDispatchValidator(charm, cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "No alertmanager_dispatch data" in schema_check.message

    def test_fails_when_unit_databag_has_no_url(self) -> None:
        # GIVEN a unit with a databag that carries neither 'url' nor 'public_address'
        validator = _make_validator({"alertmanager-k8s/0": {"other": "field"}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "public_address" in schema_check.message

    def test_fails_when_url_has_unsupported_scheme(self) -> None:
        # GIVEN a URL with an ftp:// scheme
        bad_url = "ftp://alertmanager-0.am-test.svc.cluster.local:9093"
        validator = _make_validator({"alertmanager-k8s/0": {"url": bad_url}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "unsupported scheme" in schema_check.message

    def test_fails_when_receiver_present_but_empty(self) -> None:
        # GIVEN a valid url but an empty 'receiver' advertised alongside it
        validator = _make_validator({"alertmanager-k8s/0": {"url": _URL, "receiver": "  "}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "receiver" in schema_check.message

    def test_accepts_v0_public_address_and_scheme(self) -> None:
        # GIVEN a v0-style provider databag (public_address + scheme)
        validator = _make_validator(
            {"alertmanager-k8s/0": {"public_address": "alertmanager-0.am-test.svc:9093", "scheme": "http"}}
        )

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch(
                "validators.alertmanager_dispatch.validator.urlopen",
                return_value=_mock_http_response(200, b"OK"),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN the v0 databag is parsed into a valid endpoint and passes
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed


# ---------------------------------------------------------------------------
# Tests: simple level — connectivity & health (L1)
# ---------------------------------------------------------------------------


class TestAlertmanagerDispatchValidatorSimple:
    def test_fails_when_tcp_connection_refused(self) -> None:
        # GIVEN schema is valid but TCP connection fails
        validator = _make_validator()

        with patch(
            "validators.alertmanager_dispatch.validator._tcp_ping",
            side_effect=ConnectionRefusedError("connection refused"),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "connection refused" in connect_check.message

    def test_fails_when_health_returns_non_200(self) -> None:
        # GIVEN TCP succeeds but /-/healthy returns 503 (urlopen raises HTTPError for non-2xx)
        validator = _make_validator()

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch(
                "validators.alertmanager_dispatch.validator.urlopen",
                side_effect=_mock_http_error(503),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        health_checks = [c for c in result.checks if c.name.startswith("http_healthy[")]
        assert health_checks and not all(c.passed for c in health_checks)

    def test_passes_with_valid_endpoint(self) -> None:
        # GIVEN a valid endpoint, reachable TCP, and Alertmanager healthy
        validator = _make_validator()

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch(
                "validators.alertmanager_dispatch.validator.urlopen",
                return_value=_mock_http_response(200, b"OK"),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert connect_check.passed
        health_checks = [c for c in result.checks if c.name.startswith("http_healthy[")]
        assert health_checks and all(c.passed for c in health_checks)

    def test_simple_level_has_no_canary_check(self) -> None:
        # GIVEN simple level — canary checks should not appear
        validator = _make_validator()

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch(
                "validators.alertmanager_dispatch.validator.urlopen",
                return_value=_mock_http_response(200, b"OK"),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert not any(c.name.startswith("canary[") for c in result.checks)

    def test_result_contains_endpoint_and_interface(self) -> None:
        # GIVEN
        validator = _make_validator(endpoint="alerting")

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch(
                "validators.alertmanager_dispatch.validator.urlopen",
                return_value=_mock_http_response(200, b"OK"),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "alerting"
        assert result.interface == "alertmanager_dispatch"

    def test_deduplicates_identical_urls_across_units(self) -> None:
        # GIVEN two units advertising the same URL (e.g. behind a per-app ingress)
        validator = _make_validator(
            {
                "alertmanager-k8s/0": {"url": _URL},
                "alertmanager-k8s/1": {"url": _URL},
            }
        )

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch(
                "validators.alertmanager_dispatch.validator.urlopen",
                return_value=_mock_http_response(200, b"OK"),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN schema message says 1 endpoint (deduplication), not 2
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert "1 endpoint(s)" in schema_check.message


# ---------------------------------------------------------------------------
# Tests: deep level — canary dispatch round-trip (L2)
# ---------------------------------------------------------------------------


class TestAlertmanagerDispatchValidatorDeep:
    def test_passes_when_canary_dispatched_and_found(self) -> None:
        # GIVEN /-/healthy OK, dispatch accepted, active alerts include the canary
        validator = _make_validator()
        probe = MagicMock()
        probe.hex = "abc123def456"  # matches 'validator_probe' in _ALERTS_FOUND_BODY

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch("validators.alertmanager_dispatch.validator.uuid.uuid4", return_value=probe),
            patch(
                "validators.alertmanager_dispatch.validator.urlopen",
                side_effect=[
                    _mock_http_response(200, b"OK"),  # GET /-/healthy
                    _mock_http_response(200, b""),  # POST /api/v2/alerts (dispatch)
                    _mock_http_response(200, _ALERTS_FOUND_BODY),  # GET /api/v2/alerts (query)
                    _mock_http_response(200, b""),  # POST /api/v2/alerts (resolve cleanup)
                ],
            ),
        ):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and all(c.passed for c in canary_checks)
        assert "read back" in canary_checks[0].message

    def test_fails_when_canary_not_found_after_dispatch(self) -> None:
        # GIVEN dispatch succeeds but active alerts never include the canary
        validator = _make_validator()

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch(
                "validators.alertmanager_dispatch.validator.urlopen",
                side_effect=[
                    _mock_http_response(200, b"OK"),  # GET /-/healthy
                    _mock_http_response(200, b""),  # POST dispatch
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # query attempt 1
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # query attempt 2
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # query attempt 3
                    _mock_http_response(200, b""),  # POST resolve cleanup
                ],
            ),
        ):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and not canary_checks[0].passed
        assert "not found" in canary_checks[0].message

    def test_fails_when_dispatch_rejected(self) -> None:
        # GIVEN Alertmanager rejects the dispatch with 400
        validator = _make_validator()

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch(
                "validators.alertmanager_dispatch.validator.urlopen",
                side_effect=[
                    _mock_http_response(200, b"OK"),  # GET /-/healthy
                    _mock_http_error(400),  # POST dispatch rejected
                ],
            ),
        ):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and not canary_checks[0].passed
        assert "Dispatch failed" in canary_checks[0].message
