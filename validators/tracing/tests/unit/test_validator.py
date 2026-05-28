# Copyright (C) 2026 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import json
import socket
from typing import Any, cast
from unittest.mock import patch

import ops
import pytest
from test_utils.stubs import AppStub, CharmStub, RelationStub

from validators.tracing.validator import TracingValidator

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _make_validator(databag: dict[str, str], endpoint: str = "tracing") -> TracingValidator:
    app = AppStub()
    relation = RelationStub(app=app, data={app: databag}, name=endpoint)
    charm = cast(ops.CharmBase, CharmStub(relation_name=endpoint, interface_name="tracing"))
    return TracingValidator(charm, cast(ops.Relation, relation))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OTLP_HTTP_RECEIVER = {"protocol": {"name": "otlp_http", "type": "http"}, "url": "http://tempo.example.com:4318"}
OTLP_GRPC_RECEIVER = {"protocol": {"name": "otlp_grpc", "type": "grpc"}, "url": "tempo.example.com:4317"}

VALID_DATABAG: dict[str, str] = {
    "receivers": json.dumps([OTLP_HTTP_RECEIVER]),
}

VALID_MULTI_DATABAG: dict[str, str] = {
    "receivers": json.dumps([OTLP_HTTP_RECEIVER, OTLP_GRPC_RECEIVER]),
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTracingValidatorSimple:
    def test_returns_skipped_for_unsupported_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_returns_error_when_relation_app_is_none(self) -> None:
        # GIVEN a relation whose remote app is not yet known
        relation = RelationStub(name="test-relation", app=None, data={})
        validator = TracingValidator(cast(ops.CharmBase, CharmStub()), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"

    def test_fails_schema_check_when_receivers_field_missing(self) -> None:
        # GIVEN a databag with no 'receivers' key
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "receivers" in schema_check.message

    def test_fails_schema_check_when_receivers_is_not_valid_json(self) -> None:
        # GIVEN
        validator = _make_validator({"receivers": "not-json"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "not valid JSON" in schema_check.message

    def test_fails_schema_check_when_receivers_is_empty_list(self) -> None:
        # GIVEN
        validator = _make_validator({"receivers": "[]"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed

    def test_fails_schema_check_when_receiver_missing_url(self) -> None:
        # GIVEN a receiver with no 'url' field
        bad = {"protocol": {"name": "otlp_http", "type": "http"}}
        validator = _make_validator({"receivers": json.dumps([bad])})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "url" in schema_check.message

    def test_fails_schema_check_when_protocol_type_invalid(self) -> None:
        # GIVEN a receiver with an unrecognised protocol type
        bad = {"protocol": {"name": "zipkin_http", "type": "tcp"}, "url": "http://tempo.example.com:9411"}
        validator = _make_validator({"receivers": json.dumps([bad])})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "type" in schema_check.message

    def test_fails_schema_check_when_protocol_name_missing(self) -> None:
        # GIVEN a receiver whose protocol block has no 'name'
        bad = {"protocol": {"type": "http"}, "url": "http://tempo.example.com:4318"}
        validator = _make_validator({"receivers": json.dumps([bad])})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "name" in schema_check.message

    def test_fails_schema_check_when_receiver_is_not_an_object(self) -> None:
        # GIVEN a receivers list where one entry is not a dict
        validator = _make_validator({"receivers": json.dumps(["not-an-object"])})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "not an object" in schema_check.message

    def test_fails_schema_check_when_protocol_is_not_an_object(self) -> None:
        # GIVEN a receiver whose protocol field is a string rather than a dict
        bad = {"protocol": "grpc", "url": "tempo.example.com:4317"}
        validator = _make_validator({"receivers": json.dumps([bad])})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "protocol" in schema_check.message

    def test_passes_and_reaches_single_receiver(self) -> None:
        # GIVEN a valid databag and a reachable receiver
        validator = _make_validator(VALID_DATABAG)

        with patch("validators.tracing.validator._tcp_ping"):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert connect_check.passed

    def test_passes_and_reaches_multiple_receivers(self) -> None:
        # GIVEN two receivers, both reachable
        validator = _make_validator(VALID_MULTI_DATABAG)

        with patch("validators.tracing.validator._tcp_ping"):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert connect_check.passed
        assert "2" in connect_check.message

    def test_fails_connect_check_when_receiver_unreachable(self) -> None:
        # GIVEN a valid databag but an unreachable receiver
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.tracing.validator._tcp_ping",
            side_effect=socket.timeout("timed out"),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "timed out" in connect_check.message

    def test_fails_connect_check_when_one_of_multiple_receivers_unreachable(self) -> None:
        # GIVEN two receivers but only the first responds
        validator = _make_validator(VALID_MULTI_DATABAG)

        call_count = 0

        def _selective_ping(host: str, port: int, timeout: float = 5.0) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise socket.timeout("timed out")

        with patch("validators.tracing.validator._tcp_ping", side_effect=_selective_ping):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-tracing")

        with patch("validators.tracing.validator._tcp_ping"):
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-tracing"
        assert result.interface == "tracing"

    def test_grpc_receiver_without_scheme_is_handled(self) -> None:
        # GIVEN a gRPC URL with no scheme (bare host:port, as the spec allows)
        bare_grpc = {"protocol": {"name": "otlp_grpc", "type": "grpc"}, "url": "tempo.example.com:4317"}
        validator = _make_validator({"receivers": json.dumps([bare_grpc])})

        with patch("validators.tracing.validator._tcp_ping") as mock_ping:
            result = validator.validate(level="simple")

        # THEN schema and connect both pass, and _tcp_ping was called with the right port
        assert result.status == "PASS"
        mock_ping.assert_called_once_with("tempo.example.com", 4317)

    def test_http_receiver_with_https_scheme_uses_port_443_as_default(self) -> None:
        # GIVEN an https URL with no explicit port
        https_receiver = {"protocol": {"name": "otlp_http", "type": "http"}, "url": "https://tempo.example.com"}
        validator = _make_validator({"receivers": json.dumps([https_receiver])})

        with patch("validators.tracing.validator._tcp_ping") as mock_ping:
            validator.validate(level="simple")

        mock_ping.assert_called_once_with("tempo.example.com", 443)

    def test_http_receiver_without_port_defaults_to_80(self) -> None:
        # GIVEN an http URL with no explicit port
        http_receiver = {"protocol": {"name": "otlp_http", "type": "http"}, "url": "http://tempo.example.com"}
        validator = _make_validator({"receivers": json.dumps([http_receiver])})

        with patch("validators.tracing.validator._tcp_ping") as mock_ping:
            validator.validate(level="simple")

        mock_ping.assert_called_once_with("tempo.example.com", 80)


class TestTracingValidatorDeep:
    def test_deep_passes_when_all_receivers_export_successfully(self) -> None:
        # GIVEN a valid multi-receiver databag and all network calls succeed
        validator = _make_validator(VALID_MULTI_DATABAG)

        with patch("validators.tracing.validator._tcp_ping"), patch("validators.tracing.validator._emit_test_span"):
            result = validator.validate(level="deep")

        # THEN one trace check per receiver, all pass
        assert result.status == "PASS"
        trace_checks = [c for c in result.checks if c.name.startswith("trace[")]
        assert len(trace_checks) == 2
        assert all(c.passed for c in trace_checks)

    def test_deep_fails_when_span_export_raises(self) -> None:
        # GIVEN connect succeeds but the OTLP export is rejected
        validator = _make_validator(VALID_DATABAG)

        with (
            patch("validators.tracing.validator._tcp_ping"),
            patch(
                "validators.tracing.validator._emit_test_span",
                side_effect=RuntimeError("export failed: FAILURE"),
            ),
        ):
            result = validator.validate(level="deep")

        # THEN the result is FAIL and the trace check carries the error message
        assert result.status == "FAIL"
        trace_check = next(c for c in result.checks if c.name.startswith("trace["))
        assert not trace_check.passed
        assert "export failed" in trace_check.message

    def test_deep_still_fails_on_schema_error(self) -> None:
        # GIVEN a databag missing the receivers field
        validator = _make_validator({})

        result = validator.validate(level="deep")

        # THEN schema catches it before any network checks
        assert result.status == "FAIL"
        assert not any(c.name.startswith("trace[") for c in result.checks)

    def test_deep_returns_one_trace_check_per_receiver(self) -> None:
        # GIVEN two receivers that both export successfully
        validator = _make_validator(VALID_MULTI_DATABAG)

        with patch("validators.tracing.validator._tcp_ping"), patch("validators.tracing.validator._emit_test_span"):
            result = validator.validate(level="deep")

        trace_checks = [c for c in result.checks if c.name.startswith("trace[")]
        assert len(trace_checks) == 2
        names = {c.name for c in trace_checks}
        assert "trace[otlp_http]" in names
        assert "trace[otlp_grpc]" in names

    def test_deep_check_names_include_protocol_name(self) -> None:
        # GIVEN a single HTTP receiver
        validator = _make_validator(VALID_DATABAG)

        with patch("validators.tracing.validator._tcp_ping"), patch("validators.tracing.validator._emit_test_span"):
            result = validator.validate(level="deep")

        trace_checks = [c for c in result.checks if c.name.startswith("trace[")]
        assert trace_checks[0].name == "trace[otlp_http]"

    def test_emit_test_span_uses_correct_http_path(self) -> None:
        # GIVEN the _emit_test_span helper is called with an http receiver
        # WHEN we inspect what endpoint the HTTP exporter is constructed with
        from opentelemetry.sdk.trace.export import SpanExportResult

        captured_endpoint: list[str] = []

        class FakeExporter:
            def __init__(self, endpoint: str, timeout: int) -> None:
                captured_endpoint.append(endpoint)

            def export(self, spans: Any) -> Any:
                return SpanExportResult.SUCCESS

            def shutdown(self) -> None:
                pass

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                return True

        import validators.tracing.validator as vmod

        with (
            patch.object(vmod, "_emit_test_span", wraps=vmod._emit_test_span),
            patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", FakeExporter),
        ):
            vmod._emit_test_span("http://tempo.example.com:4318", "http")

        assert captured_endpoint == ["http://tempo.example.com:4318/v1/traces"]

    def test_emit_test_span_uses_insecure_for_grpc(self) -> None:
        # GIVEN the _emit_test_span helper is called with a gRPC receiver
        # WHEN we inspect how the gRPC exporter is constructed
        from opentelemetry.sdk.trace.export import SpanExportResult

        captured_kwargs: list[dict[str, Any]] = []

        class FakeGrpcExporter:
            def __init__(self, endpoint: str, timeout: int, insecure: bool) -> None:
                captured_kwargs.append({"endpoint": endpoint, "timeout": timeout, "insecure": insecure})

            def export(self, spans: Any) -> Any:
                return SpanExportResult.SUCCESS

            def shutdown(self) -> None:
                pass

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                return True

        import validators.tracing.validator as vmod

        with patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter", FakeGrpcExporter):
            vmod._emit_test_span("tempo.example.com:4317", "grpc")

        assert len(captured_kwargs) == 1
        assert captured_kwargs[0]["insecure"] is True
        assert captured_kwargs[0]["endpoint"] == "tempo.example.com:4317"

    def test_emit_test_span_raises_with_exception_when_export_raises(self) -> None:
        # GIVEN the inner exporter raises an exception during export

        class RaisingExporter:
            def __init__(self, endpoint: str, timeout: int) -> None:
                pass

            def export(self, spans: Any) -> Any:
                raise ConnectionRefusedError("connection refused")

            def shutdown(self) -> None:
                pass

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                return True

        import validators.tracing.validator as vmod

        with (
            patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", RaisingExporter),
            pytest.raises(RuntimeError) as exc_info,
        ):
            vmod._emit_test_span("http://tempo.example.com:4318", "http")

        assert "connection refused" in str(exc_info.value)

    def test_emit_test_span_raises_with_log_message_when_exporter_returns_failure(self) -> None:
        # GIVEN the inner exporter returns FAILURE and logs a message
        import logging

        from opentelemetry.sdk.trace.export import SpanExportResult

        class LoggingFailureExporter:
            def __init__(self, endpoint: str, timeout: int) -> None:
                pass

            def export(self, spans: Any) -> Any:
                logging.getLogger("opentelemetry.exporter.otlp").error("StatusCode.UNAVAILABLE: unreachable")
                return SpanExportResult.FAILURE

            def shutdown(self) -> None:
                pass

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                return True

        import validators.tracing.validator as vmod

        with (
            patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", LoggingFailureExporter),
            pytest.raises(RuntimeError) as exc_info,
        ):
            vmod._emit_test_span("http://tempo.example.com:4318", "http")

        assert "StatusCode.UNAVAILABLE: unreachable" in str(exc_info.value)

    def test_emit_test_span_raises_with_result_name_when_no_log_and_no_exception(self) -> None:
        # GIVEN the inner exporter returns FAILURE without logging or raising
        from opentelemetry.sdk.trace.export import SpanExportResult

        class SilentFailureExporter:
            def __init__(self, endpoint: str, timeout: int) -> None:
                pass

            def export(self, spans: Any) -> Any:
                return SpanExportResult.FAILURE

            def shutdown(self) -> None:
                pass

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                return True

        import validators.tracing.validator as vmod

        with (
            patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", SilentFailureExporter),
            pytest.raises(RuntimeError) as exc_info,
        ):
            vmod._emit_test_span("http://tempo.example.com:4318", "http")

        assert "FAILURE" in str(exc_info.value)
