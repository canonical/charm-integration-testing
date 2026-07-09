# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import urllib.error
from typing import cast
from unittest.mock import MagicMock, patch

import ops

from validators.prometheus_remote_write.validator import PrometheusRemoteWriteValidator
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

_WRITE_URL = "http://prometheus-0.prometheus.svc.cluster.local:9090/api/v1/write"


def _make_validator(
    unit_databags: dict[str, dict[str, str]] | None = None,
    endpoint: str = "send-remote-write",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> PrometheusRemoteWriteValidator:
    """Create a validator with the given provider unit databags.

    *unit_databags* maps unit name → databag dict.  Defaults to a single
    unit with a valid ``remote_write`` entry.
    """
    if unit_databags is None:
        unit_databags = {"prometheus-k8s/0": {"remote_write": json.dumps({"url": _WRITE_URL})}}

    app = ApplicationStub()
    units = frozenset(UnitStub(name) for name in unit_databags)
    data: dict[ApplicationStub | UnitStub | None, dict[str, str]] = {app: {}}
    for unit in units:
        data[unit] = unit_databags[unit.name]

    relation = RelationStub(name=endpoint, id=0, app=app, data=data, units=units)
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(relation, role=role, interface_name="prometheus_remote_write"),
    )
    return PrometheusRemoteWriteValidator(charm, cast(ops.Relation, relation))


def _mock_http_response(status: int = 200, body: bytes = b"Prometheus is Ready.") -> MagicMock:
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


# ---------------------------------------------------------------------------
# Tests: role and level guards
# ---------------------------------------------------------------------------


class TestPrometheusRemoteWriteValidatorGuards:
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
        # GIVEN a relation whose app is not in its data dict
        app = ApplicationStub()
        relation = RelationStub(name="send-remote-write", id=0, app=app, data={app: {}})
        relation.app = ApplicationStub()  # different stub so relation_exists() returns False
        # We need app=None to trigger the check
        relation.app = None
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="prometheus_remote_write"),
        )
        validator = PrometheusRemoteWriteValidator(charm, cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"
        assert result.error is not None


# ---------------------------------------------------------------------------
# Tests: schema validation
# ---------------------------------------------------------------------------


class TestPrometheusRemoteWriteValidatorSchema:
    def test_fails_when_no_units(self) -> None:
        # GIVEN an empty units set → no remote_write data
        app = ApplicationStub()
        relation = RelationStub(name="send-remote-write", id=0, app=app, data={app: {}}, units=frozenset())
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="prometheus_remote_write"),
        )
        validator = PrometheusRemoteWriteValidator(charm, cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "No 'remote_write' data" in schema_check.message

    def test_fails_when_remote_write_missing_from_unit_databag(self) -> None:
        # GIVEN a unit with no 'remote_write' key
        validator = _make_validator({"prometheus-k8s/0": {}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "remote_write" in schema_check.message

    def test_fails_when_remote_write_is_not_valid_json(self) -> None:
        # GIVEN a unit with invalid JSON in 'remote_write'
        validator = _make_validator({"prometheus-k8s/0": {"remote_write": "not-json"}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "not valid JSON" in schema_check.message

    def test_fails_when_remote_write_missing_url(self) -> None:
        # GIVEN a unit with a valid JSON dict but missing 'url'
        validator = _make_validator({"prometheus-k8s/0": {"remote_write": json.dumps({"other": "field"})}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "url" in schema_check.message

    def test_fails_when_url_has_wrong_path(self) -> None:
        # GIVEN a URL that doesn't end with /api/v1/write
        bad_url = "http://prometheus-0.prometheus.svc.cluster.local:9090/metrics"
        validator = _make_validator({"prometheus-k8s/0": {"remote_write": json.dumps({"url": bad_url})}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "/api/v1/write" in schema_check.message

    def test_fails_when_url_has_unsupported_scheme(self) -> None:
        # GIVEN a URL with ftp:// scheme
        bad_url = "ftp://prometheus-0.prometheus.svc.cluster.local:9090/api/v1/write"
        validator = _make_validator({"prometheus-k8s/0": {"remote_write": json.dumps({"url": bad_url})}})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "unsupported scheme" in schema_check.message


# ---------------------------------------------------------------------------
# Tests: simple level — connectivity & readiness
# ---------------------------------------------------------------------------


class TestPrometheusRemoteWriteValidatorSimple:
    def test_fails_when_tcp_connection_refused(self) -> None:
        # GIVEN schema is valid but TCP connection fails
        validator = _make_validator()

        with patch(
            "validators.prometheus_remote_write.validator._tcp_ping",
            side_effect=ConnectionRefusedError("connection refused"),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "connection refused" in connect_check.message

    def test_fails_when_http_ready_returns_non_200(self) -> None:
        # GIVEN TCP succeeds but /-/ready returns 503
        validator = _make_validator()

        with (
            patch("validators.prometheus_remote_write.validator._tcp_ping"),
            patch("validators.prometheus_remote_write.validator.time.sleep"),
            patch(
                "validators.prometheus_remote_write.validator.urlopen",
                return_value=_mock_http_response(503, b"not ready"),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        ready_checks = [c for c in result.checks if c.name.startswith("http_ready[")]
        assert ready_checks and not all(c.passed for c in ready_checks)

    def test_passes_with_valid_endpoint(self) -> None:
        # GIVEN a complete valid endpoint, reachable TCP, and Prometheus ready
        validator = _make_validator()

        with (
            patch("validators.prometheus_remote_write.validator._tcp_ping"),
            patch(
                "validators.prometheus_remote_write.validator.urlopen",
                return_value=_mock_http_response(200, b"Prometheus is Ready."),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert connect_check.passed
        ready_checks = [c for c in result.checks if c.name.startswith("http_ready[")]
        assert ready_checks and all(c.passed for c in ready_checks)

    def test_simple_level_has_no_canary_check(self) -> None:
        # GIVEN simple level — canary checks should not appear
        validator = _make_validator()

        with (
            patch("validators.prometheus_remote_write.validator._tcp_ping"),
            patch(
                "validators.prometheus_remote_write.validator.urlopen",
                return_value=_mock_http_response(200, b"Prometheus is Ready."),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN: no canary checks at simple level
        assert not any(c.name.startswith("canary[") for c in result.checks)

    def test_result_contains_endpoint_and_interface(self) -> None:
        # GIVEN
        validator = _make_validator(endpoint="my-remote-write")

        with (
            patch("validators.prometheus_remote_write.validator._tcp_ping"),
            patch(
                "validators.prometheus_remote_write.validator.urlopen",
                return_value=_mock_http_response(200, b"Prometheus is Ready."),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-remote-write"
        assert result.interface == "prometheus_remote_write"

    def test_deduplicates_identical_urls_across_units(self) -> None:
        # GIVEN two units advertising the same URL (e.g. behind a service load balancer)
        same_url = json.dumps({"url": _WRITE_URL})
        validator = _make_validator(
            {
                "prometheus-k8s/0": {"remote_write": same_url},
                "prometheus-k8s/1": {"remote_write": same_url},
            }
        )

        with (
            patch("validators.prometheus_remote_write.validator._tcp_ping"),
            patch(
                "validators.prometheus_remote_write.validator.urlopen",
                return_value=_mock_http_response(200, b"Prometheus is Ready."),
            ),
        ):
            result = validator.validate(level="simple")

        # THEN: schema message says 1 endpoint (deduplication), not 2
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert "1 endpoint(s)" in schema_check.message


# ---------------------------------------------------------------------------
# Tests: deep level — canary write + query round-trip
# ---------------------------------------------------------------------------

_QUERY_FOUND_BODY = json.dumps(
    {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"__name__": "validator_canary", "__validator_probe__": "abc123def456"},
                    "value": [1720000000.0, "1"],
                }
            ],
        },
    }
).encode()

_QUERY_NOT_FOUND_BODY = json.dumps({"status": "success", "data": {"resultType": "vector", "result": []}}).encode()


class TestPrometheusRemoteWriteValidatorDeep:
    def test_passes_when_canary_pushed_and_found(self) -> None:
        # GIVEN /-/ready OK, push returns 204, query returns the canary metric
        validator = _make_validator()

        with (
            patch("validators.prometheus_remote_write.validator._tcp_ping"),
            patch("validators.prometheus_remote_write.validator.time") as mock_time,
            patch(
                "validators.prometheus_remote_write.validator.urlopen",
                side_effect=[
                    _mock_http_response(200, b"Prometheus is Ready."),  # /-/ready
                    _mock_http_response(204, b""),  # POST /api/v1/write
                    _mock_http_response(200, _QUERY_FOUND_BODY),  # GET /api/v1/query
                ],
            ),
        ):
            mock_time.time.return_value = 1720000000.0
            mock_time.sleep = lambda _: None
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and all(c.passed for c in canary_checks)
        assert "written and queried back" in canary_checks[0].message

    def test_fails_when_canary_not_found_after_push(self) -> None:
        # GIVEN push succeeds but query returns an empty result (metric not ingested)
        validator = _make_validator()

        with (
            patch("validators.prometheus_remote_write.validator._tcp_ping"),
            patch("validators.prometheus_remote_write.validator.time") as mock_time,
            patch(
                "validators.prometheus_remote_write.validator.urlopen",
                side_effect=[
                    _mock_http_response(200, b"Prometheus is Ready."),
                    _mock_http_response(204, b""),
                    _mock_http_response(200, _QUERY_NOT_FOUND_BODY),
                ],
            ),
        ):
            mock_time.time.return_value = 1720000000.0
            mock_time.sleep = lambda _: None
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and not canary_checks[0].passed
        assert "not found" in canary_checks[0].message

    def test_fails_when_push_rejected(self) -> None:
        # GIVEN the remote write endpoint rejects the push with 400
        validator = _make_validator()

        with (
            patch("validators.prometheus_remote_write.validator._tcp_ping"),
            patch("validators.prometheus_remote_write.validator.time") as mock_time,
            patch(
                "validators.prometheus_remote_write.validator.urlopen",
                side_effect=[
                    _mock_http_response(200, b"Prometheus is Ready."),
                    _mock_http_error(400),  # push rejected
                ],
            ),
        ):
            mock_time.time.return_value = 1720000000.0
            mock_time.sleep = lambda _: None
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and not canary_checks[0].passed
        assert "Push failed" in canary_checks[0].message

    def test_fails_when_query_errors(self) -> None:
        # GIVEN push succeeds but the query API raises a connection error
        validator = _make_validator()

        with (
            patch("validators.prometheus_remote_write.validator._tcp_ping"),
            patch("validators.prometheus_remote_write.validator.time") as mock_time,
            patch(
                "validators.prometheus_remote_write.validator.urlopen",
                side_effect=[
                    _mock_http_response(200, b"Prometheus is Ready."),
                    _mock_http_response(204, b""),
                    ConnectionRefusedError("refused"),
                ],
            ),
        ):
            mock_time.time.return_value = 1720000000.0
            mock_time.sleep = lambda _: None
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        canary_checks = [c for c in result.checks if c.name.startswith("canary[")]
        assert canary_checks and not canary_checks[0].passed
        assert "Query failed" in canary_checks[0].message

    def test_schema_fail_stops_before_canary(self) -> None:
        # GIVEN empty unit databags — schema fails, canary should not run
        validator = _make_validator({"prometheus-k8s/0": {}})

        with patch("validators.prometheus_remote_write.validator.urlopen") as mock_urlopen:
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: encoding helpers
# ---------------------------------------------------------------------------


class TestPrometheusRemoteWriteEncoding:
    def test_build_write_request_returns_bytes(self) -> None:
        from validators.prometheus_remote_write.validator import _build_write_request

        # GIVEN
        payload = _build_write_request("validator_canary", "abc123", 1.0, 1720000000000)

        # THEN: non-empty bytes
        assert isinstance(payload, bytes) and len(payload) > 0

    def test_snappy_compress_length_header_is_correct(self) -> None:
        from validators.prometheus_remote_write.validator import _encode_varint, _snappy_compress

        # GIVEN arbitrary input
        data = b"hello snappy world"

        # WHEN
        compressed = _snappy_compress(data)

        # THEN: first byte(s) encode the original length as a varint
        assert compressed[: len(_encode_varint(len(data)))] == _encode_varint(len(data))

    def test_snappy_compress_roundtrip_via_framing(self) -> None:
        """Verify the compressed output is accepted by the snappy library if available."""
        try:
            import snappy  # type: ignore[import-not-found]
        except ImportError:
            return  # snappy not installed — skip without failing

        from validators.prometheus_remote_write.validator import _build_write_request, _snappy_compress

        payload = _build_write_request("validator_canary", "probe123abc", 1.0, 1720000000000)
        compressed = _snappy_compress(payload)
        decompressed = snappy.uncompress(compressed)
        assert decompressed == payload
