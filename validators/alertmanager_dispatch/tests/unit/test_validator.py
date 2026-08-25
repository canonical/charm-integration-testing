# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import urllib.error
from datetime import datetime, timezone
from typing import cast
from unittest.mock import MagicMock, patch
from urllib.request import HTTPRedirectHandler

import ops

from validators.alertmanager_dispatch.validator import (
    _RESOLVE_CONFIRM_ATTEMPTS,
    _SILENCE_SETTLE_ATTEMPTS,
    AlertmanagerDispatchValidator,
    _NoRedirectHandler,
)
from validators.alertmanager_dispatch.validator import urlopen as am_urlopen
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

_SILENCE_CREATED_BODY = json.dumps({"silenceID": "sil-abc123"}).encode()
_SILENCE_ACTIVE_BODY = json.dumps({"id": "sil-abc123", "status": {"state": "active"}}).encode()
_SILENCE_PENDING_BODY = json.dumps({"id": "sil-abc123", "status": {"state": "pending"}}).encode()


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

    def test_fails_when_a_scaled_unit_has_empty_databag(self) -> None:
        # GIVEN one provider unit advertises a valid url and a scaled sibling has an empty databag
        validator = _make_validator(
            {
                "alertmanager-k8s/0": {"url": _URL},
                "alertmanager-k8s/1": {},
            }
        )

        # WHEN
        result = validator.validate(level="simple")

        # THEN the empty databag is surfaced as a schema error instead of being silently ignored
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "alertmanager-k8s/1" in schema_check.message
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

    def test_fails_when_url_has_malformed_port(self) -> None:
        # GIVEN a URL whose port is out of range -> urlparse().port raises ValueError
        validator = _make_validator({"alertmanager-k8s/0": {"url": "http://alertmanager-0.am-test.svc:99999"}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN the parse error is reported as a failed schema check, not raised out of validate()
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "malformed URL" in schema_check.message

    def test_fails_when_url_has_unmatched_ipv6_bracket(self) -> None:
        # GIVEN a URL with an unterminated IPv6 literal -> urlparse().hostname raises ValueError
        validator = _make_validator({"alertmanager-k8s/0": {"url": "http://[::1:9093"}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "malformed URL" in schema_check.message

    def test_fails_when_url_has_query_or_fragment(self) -> None:
        # GIVEN a URL with a query component; API paths are appended by concatenation so this would misroute
        validator = _make_validator({"alertmanager-k8s/0": {"url": "http://alertmanager-0.am-test.svc:9093?x"}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN it is rejected instead of silently targeting '/' with a query
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "query or fragment" in schema_check.message

    def test_fails_when_url_has_empty_query_delimiter(self) -> None:
        # GIVEN a URL ending in a bare '?'; urlparse drops the empty query, but appending an API path would misroute
        validator = _make_validator({"alertmanager-k8s/0": {"url": "http://alertmanager-0.am-test.svc:9093?"}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN the bare delimiter is still rejected
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "query or fragment" in schema_check.message

    def test_fails_when_url_has_empty_fragment_delimiter(self) -> None:
        # GIVEN a URL ending in a bare '#'; urlparse drops the empty fragment, but an API path would hide in it
        validator = _make_validator({"alertmanager-k8s/0": {"url": "http://alertmanager-0.am-test.svc:9093#"}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN the bare delimiter is still rejected
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "query or fragment" in schema_check.message

    def test_fails_when_v0_scheme_explicitly_empty(self) -> None:
        # GIVEN a v0 databag with an explicitly empty 'scheme' (malformed data)
        validator = _make_validator(
            {"alertmanager-k8s/0": {"public_address": "alertmanager-0.am-test.svc:9093", "scheme": ""}}
        )

        # WHEN
        result = validator.validate(level="simple")

        # THEN the empty scheme is preserved (not defaulted to http) and rejected
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "unsupported scheme" in schema_check.message

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

    def test_fails_when_health_endpoint_redirects(self) -> None:
        # GIVEN /-/healthy answers with a redirect (e.g. an auth proxy bouncing to a login page);
        # the no-redirect opener surfaces the 302 as an HTTPError instead of following it to a 200
        validator = _make_validator()

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch(
                "validators.alertmanager_dispatch.validator.urlopen",
                side_effect=_mock_http_error(302),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN the redirect is not treated as healthy
        assert result.status == "FAIL"
        health_check = next(c for c in result.checks if c.name.startswith("http_healthy["))
        assert not health_check.passed
        assert "302" in health_check.message

    def test_module_urlopen_rejects_redirects(self) -> None:
        # GIVEN the module HTTP client is built from a no-redirect opener
        opener = am_urlopen.__self__  # type: ignore[attr-defined]
        redirect_handlers = [h for h in opener.handlers if isinstance(h, HTTPRedirectHandler)]

        # THEN every installed redirect handler is the no-redirect variant and suppresses redirects
        assert redirect_handlers
        assert all(isinstance(h, _NoRedirectHandler) for h in redirect_handlers)
        assert all(
            h.redirect_request(MagicMock(), MagicMock(), 302, "Found", MagicMock(), "http://example/login") is None
            for h in redirect_handlers
        )

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
                    _mock_http_response(200, _SILENCE_CREATED_BODY),  # POST /api/v2/silences (silence)
                    _mock_http_response(200, _SILENCE_ACTIVE_BODY),  # GET /api/v2/silence/{id} (confirm active)
                    _mock_http_response(200, b""),  # POST /api/v2/alerts (dispatch)
                    _mock_http_response(200, _ALERTS_FOUND_BODY),  # GET /api/v2/alerts (query)
                    _mock_http_response(200, b""),  # POST /api/v2/alerts (resolve cleanup)
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # GET /api/v2/alerts (confirm resolved)
                    _mock_http_response(200, b""),  # DELETE /api/v2/silences/{id}
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
                    _mock_http_response(200, _SILENCE_CREATED_BODY),  # POST silence
                    _mock_http_response(200, _SILENCE_ACTIVE_BODY),  # GET silence status (confirm active)
                    _mock_http_response(200, b""),  # POST dispatch
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # query attempt 1
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # query attempt 2
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # query attempt 3
                    _mock_http_response(200, b""),  # POST resolve cleanup
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # GET confirm resolved
                    _mock_http_response(200, b""),  # DELETE silence
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
                    _mock_http_response(200, _SILENCE_CREATED_BODY),  # POST silence
                    _mock_http_response(200, _SILENCE_ACTIVE_BODY),  # GET silence status (confirm active)
                    _mock_http_error(400),  # POST dispatch rejected
                    _mock_http_response(200, b""),  # POST resolve cleanup
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # GET confirm resolved
                    _mock_http_response(200, b""),  # DELETE silence
                ],
            ),
        ):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and not canary_checks[0].passed
        assert "Dispatch failed" in canary_checks[0].message

    def test_reports_not_found_when_early_query_errors_then_succeeds(self) -> None:
        # GIVEN dispatch succeeds, the first query attempt raises, and a later
        # attempt succeeds but returns no matching canary
        validator = _make_validator()

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch(
                "validators.alertmanager_dispatch.validator.urlopen",
                side_effect=[
                    _mock_http_response(200, b"OK"),  # GET /-/healthy
                    _mock_http_response(200, _SILENCE_CREATED_BODY),  # POST silence
                    _mock_http_response(200, _SILENCE_ACTIVE_BODY),  # GET silence status (confirm active)
                    _mock_http_response(200, b""),  # POST dispatch
                    _mock_http_error(502),  # query attempt 1 raises a transient error
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # query attempt 2 -> empty
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # query attempt 3 -> empty
                    _mock_http_response(200, b""),  # POST resolve cleanup
                    _mock_http_response(200, _ALERTS_EMPTY_BODY),  # GET confirm resolved
                    _mock_http_response(200, b""),  # DELETE silence
                ],
            ),
        ):
            result = validator.validate(level="deep")

        # THEN the later successful query supersedes the transient error, so the
        # message reports "not found" rather than the earlier "Query failed"
        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and not canary_checks[0].passed
        assert "not found" in canary_checks[0].message
        assert "Query failed" not in canary_checks[0].message

    def test_query_uses_server_side_filter_matcher(self) -> None:
        # GIVEN a deep run where the canary is found on the first query
        validator = _make_validator()
        probe = MagicMock()
        probe.hex = "abc123def456"  # matches 'validator_probe' in _ALERTS_FOUND_BODY
        urlopen = MagicMock(
            side_effect=[
                _mock_http_response(200, b"OK"),  # GET /-/healthy
                _mock_http_response(200, _SILENCE_CREATED_BODY),  # POST /api/v2/silences (silence)
                _mock_http_response(200, _SILENCE_ACTIVE_BODY),  # GET /api/v2/silence/{id} (confirm active)
                _mock_http_response(200, b""),  # POST /api/v2/alerts (dispatch)
                _mock_http_response(200, _ALERTS_FOUND_BODY),  # GET /api/v2/alerts (query)
                _mock_http_response(200, b""),  # POST /api/v2/alerts (resolve cleanup)
                _mock_http_response(200, _ALERTS_EMPTY_BODY),  # GET /api/v2/alerts (confirm resolved)
                _mock_http_response(200, b""),  # DELETE /api/v2/silences/{id}
            ]
        )

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch("validators.alertmanager_dispatch.validator.uuid.uuid4", return_value=probe),
            patch("validators.alertmanager_dispatch.validator.urlopen", urlopen),
        ):
            validator.validate(level="deep")

        # THEN the active-alerts query is scoped by a server-side filter matcher
        # on the canary's own probe label instead of downloading every alert
        query_target = urlopen.call_args_list[4].args[0]
        assert isinstance(query_target, str)
        assert "filter=validator_probe" in query_target
        assert "abc123def456" in query_target

    def test_resolve_payload_starts_before_it_ends(self) -> None:
        # GIVEN a deep run where the canary is found, triggering resolve cleanup
        validator = _make_validator()
        probe = MagicMock()
        probe.hex = "abc123def456"  # matches 'validator_probe' in _ALERTS_FOUND_BODY
        urlopen = MagicMock(
            side_effect=[
                _mock_http_response(200, b"OK"),  # GET /-/healthy
                _mock_http_response(200, _SILENCE_CREATED_BODY),  # POST /api/v2/silences (silence)
                _mock_http_response(200, _SILENCE_ACTIVE_BODY),  # GET /api/v2/silence/{id} (confirm active)
                _mock_http_response(200, b""),  # POST /api/v2/alerts (dispatch)
                _mock_http_response(200, _ALERTS_FOUND_BODY),  # GET /api/v2/alerts (query)
                _mock_http_response(200, b""),  # POST /api/v2/alerts (resolve cleanup)
                _mock_http_response(200, _ALERTS_EMPTY_BODY),  # GET /api/v2/alerts (confirm resolved)
                _mock_http_response(200, b""),  # DELETE /api/v2/silences/{id}
            ]
        )

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch("validators.alertmanager_dispatch.validator.uuid.uuid4", return_value=probe),
            patch("validators.alertmanager_dispatch.validator.urlopen", urlopen),
        ):
            validator.validate(level="deep")

        # THEN the resolve payload keeps startsAt strictly before endsAt so
        # Alertmanager accepts the cleanup and actually resolves the canary
        resolve_request = urlopen.call_args_list[5].args[0]
        payload = json.loads(resolve_request.data)
        starts_at = datetime.fromisoformat(payload[0]["startsAt"])
        ends_at = datetime.fromisoformat(payload[0]["endsAt"])
        assert starts_at < ends_at

    def test_reports_cleanup_failure_when_resolve_rejected(self) -> None:
        # GIVEN the canary is dispatched and read back, but the resolve POST is rejected
        validator = _make_validator()
        probe = MagicMock()
        probe.hex = "abc123def456"  # matches 'validator_probe' in _ALERTS_FOUND_BODY

        urlopen = MagicMock(
            side_effect=[
                _mock_http_response(200, b"OK"),  # GET /-/healthy
                _mock_http_response(200, _SILENCE_CREATED_BODY),  # POST silence
                _mock_http_response(200, _SILENCE_ACTIVE_BODY),  # GET silence status (confirm active)
                _mock_http_response(200, b""),  # POST dispatch
                _mock_http_response(200, _ALERTS_FOUND_BODY),  # GET query (found)
                _mock_http_error(400),  # POST resolve rejected
            ]
        )

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch("validators.alertmanager_dispatch.validator.uuid.uuid4", return_value=probe),
            patch("validators.alertmanager_dispatch.validator.urlopen", urlopen),
        ):
            result = validator.validate(level="deep")

        # THEN the cleanup failure is surfaced as a failing check rather than swallowed
        assert result.status == "FAIL"
        cleanup_checks = [c for c in result.checks if c.name.startswith("canary_cleanup[")]
        assert cleanup_checks and not cleanup_checks[0].passed
        assert "cleanup failed" in cleanup_checks[0].message.lower()
        # the round-trip itself still passed
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and canary_checks[0].passed
        # AND the silence is retained (no DELETE) so the unresolved canary stays muted until expiry
        methods = [call.args[0].method for call in urlopen.call_args_list if hasattr(call.args[0], "method")]
        assert "DELETE" not in methods
        assert urlopen.call_count == 6  # healthy, silence, confirm-active, dispatch, query, resolve (no delete)

    def test_retains_silence_when_canary_still_active_after_resolve(self) -> None:
        # GIVEN the resolve POST is accepted but the canary is still listed active
        # afterwards (clock skew / HA propagation), so it cannot be confirmed resolved
        validator = _make_validator()
        probe = MagicMock()
        probe.hex = "abc123def456"
        urlopen = MagicMock(
            side_effect=[
                _mock_http_response(200, b"OK"),  # GET /-/healthy
                _mock_http_response(200, _SILENCE_CREATED_BODY),  # POST silence
                _mock_http_response(200, _SILENCE_ACTIVE_BODY),  # GET silence status (confirm active)
                _mock_http_response(200, b""),  # POST dispatch
                _mock_http_response(200, _ALERTS_FOUND_BODY),  # GET query (found)
                _mock_http_response(200, b""),  # POST resolve
                # every confirm poll still finds the canary active, so it is never confirmed resolved
                *[_mock_http_response(200, _ALERTS_FOUND_BODY) for _ in range(_RESOLVE_CONFIRM_ATTEMPTS)],
            ]
        )

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch("validators.alertmanager_dispatch.validator.uuid.uuid4", return_value=probe),
            patch("validators.alertmanager_dispatch.validator.urlopen", urlopen),
        ):
            result = validator.validate(level="deep")

        # THEN the silence is retained (no DELETE) so the still-active canary stays muted
        assert result.status == "FAIL"
        cleanup_checks = [c for c in result.checks if c.name.startswith("canary_cleanup[")]
        assert cleanup_checks and not cleanup_checks[0].passed
        assert "still active" in cleanup_checks[0].message
        methods = [call.args[0].method for call in urlopen.call_args_list if hasattr(call.args[0], "method")]
        assert "DELETE" not in methods

    def test_attempts_cleanup_after_dispatch_transport_error(self) -> None:
        # GIVEN the dispatch POST raises locally (e.g. a read timeout) even though
        # it may already have reached Alertmanager
        validator = _make_validator()
        probe = MagicMock()
        probe.hex = "abc123def456"
        urlopen = MagicMock(
            side_effect=[
                _mock_http_response(200, b"OK"),  # GET /-/healthy
                _mock_http_response(200, _SILENCE_CREATED_BODY),  # POST silence
                _mock_http_response(200, _SILENCE_ACTIVE_BODY),  # GET silence status (confirm active)
                TimeoutError("read timed out"),  # POST dispatch raises after possibly reaching AM
                _mock_http_response(200, b""),  # POST resolve cleanup
                _mock_http_response(200, _ALERTS_EMPTY_BODY),  # GET confirm resolved
                _mock_http_response(200, b""),  # DELETE silence
            ]
        )

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch("validators.alertmanager_dispatch.validator.uuid.uuid4", return_value=probe),
            patch("validators.alertmanager_dispatch.validator.urlopen", urlopen),
        ):
            result = validator.validate(level="deep")

        # THEN cleanup is still attempted (resolve + silence removal) despite the
        # dispatch transport error, so no canary can linger
        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and not canary_checks[0].passed
        assert "Dispatch failed" in canary_checks[0].message
        cleanup_checks = [c for c in result.checks if c.name.startswith("canary_cleanup[")]
        assert cleanup_checks and cleanup_checks[0].passed
        assert urlopen.call_count == 7  # healthy, silence, confirm-active, dispatch, resolve, confirm-resolved, delete

    def test_silences_probe_before_dispatch(self) -> None:
        # GIVEN a fully successful deep run
        validator = _make_validator()
        probe = MagicMock()
        probe.hex = "abc123def456"
        urlopen = MagicMock(
            side_effect=[
                _mock_http_response(200, b"OK"),  # GET /-/healthy
                _mock_http_response(200, _SILENCE_CREATED_BODY),  # POST /api/v2/silences
                _mock_http_response(200, _SILENCE_ACTIVE_BODY),  # GET /api/v2/silence/{id} (confirm active)
                _mock_http_response(200, b""),  # POST /api/v2/alerts (dispatch)
                _mock_http_response(200, _ALERTS_FOUND_BODY),  # GET /api/v2/alerts (query)
                _mock_http_response(200, b""),  # POST /api/v2/alerts (resolve)
                _mock_http_response(200, _ALERTS_EMPTY_BODY),  # GET /api/v2/alerts (confirm resolved)
                _mock_http_response(200, b""),  # DELETE /api/v2/silences/{id}
            ]
        )

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch("validators.alertmanager_dispatch.validator.uuid.uuid4", return_value=probe),
            patch("validators.alertmanager_dispatch.validator.urlopen", urlopen),
        ):
            result = validator.validate(level="deep")

        # THEN the probe label is silenced (POST /api/v2/silences) before the
        # dispatch (POST /api/v2/alerts), and the silence is removed in cleanup
        silence_request = urlopen.call_args_list[1].args[0]
        dispatch_request = urlopen.call_args_list[3].args[0]
        assert silence_request.full_url.endswith("/api/v2/silences")
        assert silence_request.method == "POST"
        assert dispatch_request.full_url.endswith("/api/v2/alerts")
        silence_body = json.loads(silence_request.data)
        assert silence_body["matchers"][0] == {
            "name": "validator_probe",
            "value": "abc123def456",
            "isRegex": False,
            "isEqual": True,
        }
        # startsAt is backdated before endsAt so a leading host clock cannot leave the silence pending
        silence_start = datetime.fromisoformat(silence_body["startsAt"])
        silence_end = datetime.fromisoformat(silence_body["endsAt"])
        assert silence_start < datetime.now(timezone.utc)
        assert silence_start < silence_end
        delete_request = urlopen.call_args_list[7].args[0]
        assert delete_request.method == "DELETE"
        assert delete_request.full_url.endswith("/api/v2/silence/sil-abc123")
        assert result.status == "PASS"

    def test_skips_dispatch_when_silence_creation_fails(self) -> None:
        # GIVEN silencing the probe label fails before any alert is dispatched
        validator = _make_validator()
        probe = MagicMock()
        probe.hex = "abc123def456"
        urlopen = MagicMock(
            side_effect=[
                _mock_http_response(200, b"OK"),  # GET /-/healthy
                _mock_http_error(400),  # POST /api/v2/silences rejected
            ]
        )

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch("validators.alertmanager_dispatch.validator.uuid.uuid4", return_value=probe),
            patch("validators.alertmanager_dispatch.validator.urlopen", urlopen),
        ):
            result = validator.validate(level="deep")

        # THEN no alert is dispatched (an unsilenced canary could page real
        # receivers) and the failure is reported
        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and not canary_checks[0].passed
        assert "silence" in canary_checks[0].message.lower()
        assert urlopen.call_count == 2  # healthy + failed silence attempt, no dispatch
        assert not any(c.name.startswith("canary_cleanup[") for c in result.checks)

    def test_skips_dispatch_when_silence_response_is_not_a_dict(self) -> None:
        # GIVEN the silence POST returns valid JSON that is not an object (e.g. a list or error shape)
        validator = _make_validator()
        probe = MagicMock()
        probe.hex = "abc123def456"
        urlopen = MagicMock(
            side_effect=[
                _mock_http_response(200, b"OK"),  # GET /-/healthy
                _mock_http_response(200, json.dumps([]).encode()),  # POST /api/v2/silences -> non-dict JSON
            ]
        )

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch("validators.alertmanager_dispatch.validator.uuid.uuid4", return_value=probe),
            patch("validators.alertmanager_dispatch.validator.urlopen", urlopen),
        ):
            result = validator.validate(level="deep")

        # THEN it fails cleanly (no AttributeError) with a clear message and never dispatches
        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and not canary_checks[0].passed
        assert "did not include a silence ID" in canary_checks[0].message
        assert urlopen.call_count == 2  # healthy + silence POST only, no dispatch
        assert not any(c.name.startswith("canary_cleanup[") for c in result.checks)

    def test_skips_dispatch_when_silence_never_active(self) -> None:
        # GIVEN the silence is accepted but every settle poll reports 'pending'
        validator = _make_validator()
        probe = MagicMock()
        probe.hex = "abc123def456"
        urlopen = MagicMock(
            side_effect=[
                _mock_http_response(200, b"OK"),  # GET /-/healthy
                _mock_http_response(200, _SILENCE_CREATED_BODY),  # POST silence
                # the silence never settles to 'active' within the poll budget
                *[_mock_http_response(200, _SILENCE_PENDING_BODY) for _ in range(_SILENCE_SETTLE_ATTEMPTS)],
                _mock_http_response(200, b""),  # DELETE silence (stray state removed since no canary was dispatched)
            ]
        )

        with (
            patch("validators.alertmanager_dispatch.validator._tcp_ping"),
            patch("validators.alertmanager_dispatch.validator.time.sleep"),
            patch("validators.alertmanager_dispatch.validator.uuid.uuid4", return_value=probe),
            patch("validators.alertmanager_dispatch.validator.urlopen", urlopen),
        ):
            result = validator.validate(level="deep")

        # THEN the canary is never dispatched (no POST to /api/v2/alerts) and the check fails
        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and not canary_checks[0].passed
        assert "not confirmed active" in canary_checks[0].message
        dispatched = [
            call
            for call in urlopen.call_args_list
            if hasattr(call.args[0], "full_url") and call.args[0].full_url.endswith("/api/v2/alerts")
        ]
        assert not dispatched
        # AND the accepted-but-unconfirmed silence is removed rather than left as stray state
        deleted = [
            call
            for call in urlopen.call_args_list
            if hasattr(call.args[0], "method") and call.args[0].method == "DELETE"
        ]
        assert deleted
        cleanup_checks = [c for c in result.checks if c.name.startswith("canary_cleanup[")]
        assert cleanup_checks and all(c.passed for c in cleanup_checks)
