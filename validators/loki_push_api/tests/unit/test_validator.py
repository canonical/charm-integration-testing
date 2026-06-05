# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import socket
from typing import Any, cast
from unittest.mock import MagicMock, call, patch

import ops
import pytest

from validators.loki_push_api.validator import LokiPushApiValidator
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

LOKI_NETLOC = "loki-k8s-0.loki-k8s-endpoints.loki-push-api-test.svc.cluster.local:3100"
LOKI_URL = f"http://{LOKI_NETLOC}/loki/api/v1/push"

VALID_UNIT_DATABAG: dict[str, str] = {
    "endpoint": json.dumps({"url": LOKI_URL}),
}

VALID_UNIT_DATABAG_WITH_LABELS: dict[str, str] = {
    "endpoint": json.dumps({"url": LOKI_URL, "labels": json.dumps({"app": "myapp"})}),
}


def _make_relation(
    unit_databags: list[dict[str, str]],
    endpoint: str = "logging-consumer",
    app: ApplicationStub | None = None,
) -> RelationStub:
    if app is None:
        app = ApplicationStub()
    units = [UnitStub(name=f"loki-k8s/{i}") for i in range(len(unit_databags))]
    data: dict[ApplicationStub | UnitStub | None, dict[str, str]] = {app: {}}
    for unit, databag in zip(units, unit_databags):
        data[unit] = databag
    return RelationStub(name=endpoint, id=0, app=app, data=data, units=frozenset(units))


def _make_validator(
    unit_databags: list[dict[str, str]],
    endpoint: str = "logging-consumer",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> LokiPushApiValidator:
    relation = _make_relation(unit_databags, endpoint=endpoint)
    charm = make_charm_from_relation(relation, role=role, interface_name="loki_push_api")
    return LokiPushApiValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def _mock_ready_response(status: int = 200, body: bytes = b"ready") -> MagicMock:
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.status = status
    resp.read.return_value = body
    return resp


# ---------------------------------------------------------------------------
# Simple level tests
# ---------------------------------------------------------------------------


class TestLokiPushApiValidatorSimple:
    @pytest.mark.parametrize(
        "role,should_skip",
        [(RelationRoleStub.requires, False), (RelationRoleStub.provides, True), (RelationRoleStub.peer, True)],
    )
    def test_returns_skipped_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN
        validator = _make_validator([VALID_UNIT_DATABAG], role=role)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

    def test_returns_skipped_for_unsupported_level(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG])
        result = validator.validate(level="uat")
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_returns_error_when_relation_app_is_none(self) -> None:
        # GIVEN a relation with no remote application (real ops sets app=None, units={}, data={})
        relation = RelationStub(name="logging-consumer", id=0, app=None)
        charm = make_charm_from_relation(
            RelationStub(name="logging-consumer", id=0, app=ApplicationStub()),
            role=RelationRoleStub.requires,
            interface_name="loki_push_api",
        )
        validator = LokiPushApiValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))
        result = validator.validate(level="simple")
        assert result.status == "ERROR"

    def test_fails_schema_when_no_endpoint_data(self) -> None:
        validator = _make_validator([{}])
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "endpoint" in schema.message

    def test_fails_schema_when_malformed_json_endpoint(self) -> None:
        bad = {"endpoint": "not-json"}
        validator = _make_validator([bad])
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "not valid JSON" in schema.message

    def test_fails_schema_when_url_is_not_a_string(self) -> None:
        bad = {"endpoint": json.dumps({"url": 12345})}
        validator = _make_validator([bad])
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "string" in schema.message

    def test_fails_schema_but_reports_both_good_and_bad_units(self) -> None:
        bad_unit = {"endpoint": "not-json"}
        validator = _make_validator([VALID_UNIT_DATABAG, bad_unit])
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "not valid JSON" in schema.message

    def test_fails_schema_when_url_missing_push_path(self) -> None:
        bad = {"endpoint": json.dumps({"url": "http://loki:3100/"})}
        validator = _make_validator([bad])
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "/loki/api/v1/push" in schema.message

    def test_fails_schema_when_url_has_invalid_scheme(self) -> None:
        bad = {"endpoint": json.dumps({"url": "ftp://loki:3100/loki/api/v1/push"})}
        validator = _make_validator([bad])
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed

    def test_fails_schema_when_labels_is_not_a_dict(self) -> None:
        bad = {"endpoint": json.dumps({"url": LOKI_URL, "labels": json.dumps(["not", "a", "dict"])})}
        validator = _make_validator([bad])
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "labels" in schema.message

    def test_passes_schema_with_optional_labels(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG_WITH_LABELS])
        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch("validators.loki_push_api.validator.urlopen", return_value=_mock_ready_response()),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            result = validator.validate(level="simple")
        assert result.status == "PASS"
        schema = next(c for c in result.checks if c.name == "schema")
        assert schema.passed

    def test_fails_schema_when_https_tls_skip_invalid(self) -> None:
        bad = {"endpoint": json.dumps({"url": "https://loki:443/loki/api/v1/push", "tls_insecure_skip_verify": "yes"})}
        validator = _make_validator([bad])
        result = validator.validate(level="simple")
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "tls_insecure_skip_verify" in schema.message

    def test_passes_with_valid_endpoint(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG])
        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch("validators.loki_push_api.validator.urlopen", return_value=_mock_ready_response()),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            result = validator.validate(level="simple")
        assert result.status == "PASS"

    def test_result_includes_requires_role(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG], role=RelationRoleStub.requires)
        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch("validators.loki_push_api.validator.urlopen", return_value=_mock_ready_response()),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            result = validator.validate(level="simple")
        assert result.role == "requires"

    def test_fails_connect_check_when_tcp_unreachable(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG])
        with patch(
            "validators.loki_push_api.validator._tcp_ping",
            side_effect=socket.timeout("timed out"),
        ):
            result = validator.validate(level="simple")
        assert result.status == "FAIL"
        connect = next(c for c in result.checks if c.name == "connect")
        assert not connect.passed

    def test_fails_http_ready_when_loki_not_ready(self) -> None:
        from urllib.error import HTTPError

        validator = _make_validator([VALID_UNIT_DATABAG])
        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch(
                "validators.loki_push_api.validator.urlopen",
                side_effect=HTTPError(LOKI_URL, 503, "Service Unavailable", {}, None),  # type: ignore[arg-type]
            ),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            result = validator.validate(level="simple")
        assert result.status == "FAIL"
        ready = next(c for c in result.checks if c.name.startswith("http_ready"))
        assert not ready.passed

    def test_http_ready_passes_after_retry(self) -> None:
        from urllib.error import HTTPError

        validator = _make_validator([VALID_UNIT_DATABAG])
        attempt = 0

        def urlopen_side(url: object, **kw: object) -> MagicMock:
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise HTTPError(str(url), 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
            return _mock_ready_response()

        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch("validators.loki_push_api.validator.urlopen", side_effect=urlopen_side),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            result = validator.validate(level="simple")
        assert result.status == "PASS"
        assert attempt == 2

    def test_deduplicates_endpoints_across_units(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG, VALID_UNIT_DATABAG])
        with (
            patch("validators.loki_push_api.validator._tcp_ping") as mock_ping,
            patch("validators.loki_push_api.validator.urlopen", return_value=_mock_ready_response()),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            result = validator.validate(level="simple")
        assert mock_ping.call_count == 1
        assert result.status == "PASS"

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG], endpoint="my-logging")
        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch("validators.loki_push_api.validator.urlopen", return_value=_mock_ready_response()),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            result = validator.validate(level="simple")
        assert result.endpoint == "my-logging"
        assert result.interface == "loki_push_api"

    def test_build_ssl_context_returns_none_for_http(self) -> None:
        from validators.loki_push_api.validator import _build_ssl_context

        assert _build_ssl_context({"url": LOKI_URL}) is None

    def test_build_ssl_context_skips_verify_when_flag_set(self) -> None:
        import ssl as ssl_mod

        from validators.loki_push_api.validator import _build_ssl_context

        ctx = _build_ssl_context({"url": "https://loki:443/loki/api/v1/push", "tls_insecure_skip_verify": "true"})
        assert ctx is not None
        assert ctx.verify_mode == ssl_mod.CERT_NONE


# ---------------------------------------------------------------------------
# Deep level tests (L2 canary write + query)
# ---------------------------------------------------------------------------


class TestLokiPushApiValidatorDeep:
    def test_deep_passes_when_canary_found(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG])

        push_resp = MagicMock()
        push_resp.__enter__.return_value = push_resp
        push_resp.__exit__.return_value = False
        push_resp.status = 204

        query_resp = MagicMock()
        query_resp.__enter__.return_value = query_resp
        query_resp.__exit__.return_value = False
        query_resp.read.return_value = json.dumps(
            {"data": {"resultType": "streams", "result": [{"stream": {}, "values": []}]}}
        ).encode()

        # urlopen is called 3 times: /ready, push, query
        urlopen_returns = [_mock_ready_response(), push_resp, query_resp]

        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch("validators.loki_push_api.validator.urlopen", side_effect=urlopen_returns),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "PASS"
        canary = next(c for c in result.checks if c.name.startswith("canary"))
        assert canary.passed

    def test_deep_fails_when_canary_push_fails(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG])

        from urllib.error import HTTPError

        urlopen_returns = [
            _mock_ready_response(),
            HTTPError(LOKI_URL, 500, "Internal Server Error", {}, None),  # type: ignore[arg-type]
        ]

        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch("validators.loki_push_api.validator.urlopen", side_effect=urlopen_returns),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        canary = next(c for c in result.checks if c.name.startswith("canary"))
        assert not canary.passed
        assert "Push failed" in canary.message

    def test_deep_fails_when_canary_not_found_in_query(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG])

        push_resp = MagicMock()
        push_resp.__enter__.return_value = push_resp
        push_resp.__exit__.return_value = False
        push_resp.status = 204

        query_resp = MagicMock()
        query_resp.__enter__.return_value = query_resp
        query_resp.__exit__.return_value = False
        # Empty result list — canary not found
        query_resp.read.return_value = json.dumps({"data": {"resultType": "streams", "result": []}}).encode()

        urlopen_returns = [_mock_ready_response(), push_resp, query_resp]

        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch("validators.loki_push_api.validator.urlopen", side_effect=urlopen_returns),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        canary = next(c for c in result.checks if c.name.startswith("canary"))
        assert not canary.passed
        assert "not found" in canary.message

    def test_deep_skips_canary_when_ready_fails(self) -> None:
        from urllib.error import HTTPError

        validator = _make_validator([VALID_UNIT_DATABAG])

        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch(
                "validators.loki_push_api.validator.urlopen",
                side_effect=HTTPError(LOKI_URL, 503, "Service Unavailable", {}, None),  # type: ignore[arg-type]
            ),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary")]
        assert len(canary_checks) == 0

    def test_deep_canary_uses_unique_probe_id(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG])
        captured_payloads: list[bytes] = []

        def fake_urlopen(req: object, **kw: object) -> MagicMock:
            from urllib.request import Request as Req

            resp = MagicMock()
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False

            if isinstance(req, Req) and req.data:
                # Push call
                captured_payloads.append(cast(bytes, req.data))
                resp.status = 204
                return resp

            # String URL — /ready or query
            url_str = str(req)
            if "ready" in url_str:
                resp.status = 200
                resp.read.return_value = b"ready"
            else:
                resp.status = 200
                resp.read.return_value = json.dumps(
                    {"data": {"resultType": "streams", "result": [{"stream": {}, "values": []}]}}
                ).encode()
            return resp

        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch("validators.loki_push_api.validator.urlopen", side_effect=fake_urlopen),
            patch("validators.loki_push_api.validator.time.sleep"),
        ):
            validator.validate(level="deep")
            validator.validate(level="deep")

        assert len(captured_payloads) == 2
        probe_ids = [json.loads(p)["streams"][0]["stream"]["__validator_probe__"] for p in captured_payloads]
        assert probe_ids[0] != probe_ids[1]

    def test_canary_sleep_is_called_for_ingest_wait(self) -> None:
        validator = _make_validator([VALID_UNIT_DATABAG])

        push_resp = MagicMock()
        push_resp.__enter__.return_value = push_resp
        push_resp.__exit__.return_value = False
        push_resp.status = 204

        query_resp = MagicMock()
        query_resp.__enter__.return_value = query_resp
        query_resp.__exit__.return_value = False
        query_resp.read.return_value = json.dumps({"data": {"resultType": "streams", "result": [{}]}}).encode()

        urlopen_returns = [_mock_ready_response(), push_resp, query_resp]
        sleep_calls: list[Any] = []

        with (
            patch("validators.loki_push_api.validator._tcp_ping"),
            patch("validators.loki_push_api.validator.urlopen", side_effect=urlopen_returns),
            patch("validators.loki_push_api.validator.time.sleep", side_effect=lambda s: sleep_calls.append(call(s))),
        ):
            validator.validate(level="deep")

        from validators.loki_push_api.validator import _INGEST_WAIT_S

        assert call(_INGEST_WAIT_S) in sleep_calls
