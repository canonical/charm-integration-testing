# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import json
import socket
from typing import Any, Literal
from urllib.parse import urlparse

from validators.base import (  # type: ignore[import-not-found]
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)


class TracingValidator(BaseValidator):  # type: ignore[misc]
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if self.relation.app is None:
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        databag = dict(self.relation.data[self.relation.app])

        schema_check, receivers = _parse_receivers(databag)
        checks: list[ValidationCheck] = [schema_check]
        if not schema_check.passed:
            return self._fail_result(level, checks)

        checks.append(_connectivity_check(receivers))
        if level == "deep":
            checks.extend(_trace_checks(receivers))

        status: Literal["PASS", "FAIL"] = "PASS" if all(c.passed for c in checks) else "FAIL"
        return self._make_result(status, level, checks)


# ---------------------------------------------------------------------------
# Pure helpers — schema & connectivity
# ---------------------------------------------------------------------------


def _parse_receivers(databag: dict[str, str]) -> tuple[ValidationCheck, list[dict[str, Any]]]:
    """Parse and structurally validate the ``receivers`` databag field.

    Returns a *(check, receivers)* tuple. If the check did not pass,
    ``receivers`` will be an empty list.
    """
    receivers_raw = databag.get("receivers")
    if not receivers_raw:
        return (
            ValidationCheck(name="schema", passed=False, message="Missing 'receivers' field in provider databag."),
            [],
        )

    try:
        receivers = json.loads(receivers_raw)
    except json.JSONDecodeError as exc:
        return (
            ValidationCheck(name="schema", passed=False, message=f"'receivers' is not valid JSON: {exc}"),
            [],
        )

    if not isinstance(receivers, list) or not receivers:
        return (
            ValidationCheck(name="schema", passed=False, message="'receivers' must be a non-empty list."),
            [],
        )

    invalid: list[str] = []
    for i, r in enumerate(receivers):
        if not isinstance(r, dict):
            invalid.append(f"[{i}] is not an object")
            continue
        protocol = r.get("protocol", {})
        if not isinstance(protocol, dict):
            invalid.append(f"[{i}].protocol is not an object")
            continue
        if not protocol.get("name"):
            invalid.append(f"[{i}].protocol.name missing")
        if protocol.get("type") not in ("http", "grpc"):
            invalid.append(f"[{i}].protocol.type invalid (got {protocol.get('type')!r})")
        if not r.get("url"):
            invalid.append(f"[{i}].url missing")

    if invalid:
        return (
            ValidationCheck(name="schema", passed=False, message=f"Invalid receivers: {'; '.join(invalid)}"),
            [],
        )
    return ValidationCheck(name="schema", passed=True, message="OK"), list[dict[str, Any]](receivers)


def _connectivity_check(receivers: list[dict[str, Any]]) -> ValidationCheck:
    """TCP-ping every receiver endpoint; return a single pass/fail check."""
    errors: list[str] = []
    for r in receivers:
        url: str = r["url"]
        protocol_type: str = r["protocol"]["type"]
        try:
            host, port = _parse_host_port(url, protocol_type)
            _tcp_ping(host, port)
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    if errors:
        return ValidationCheck(name="connect", passed=False, message="; ".join(errors))
    return ValidationCheck(name="connect", passed=True, message=f"Reached {len(receivers)} receiver(s).")


# ---------------------------------------------------------------------------
# Pure helpers — deep trace emission
# ---------------------------------------------------------------------------


def _trace_checks(receivers: list[dict[str, Any]]) -> list[ValidationCheck]:
    """Emit a test span to each receiver; return one check per receiver."""
    checks: list[ValidationCheck] = []
    for r in receivers:
        url: str = r["url"]
        protocol_type: str = r["protocol"]["type"]
        protocol_name: str = r["protocol"]["name"]
        check_name = f"trace[{protocol_name}]"
        try:
            _emit_test_span(url, protocol_type)
            checks.append(ValidationCheck(name=check_name, passed=True, message=f"Span exported to {url}."))
        except Exception as exc:
            checks.append(ValidationCheck(name=check_name, passed=False, message=str(exc)))
    return checks


def _emit_test_span(url: str, protocol_type: str, timeout: int = 5) -> None:
    """Export a single minimal span to *url* and raise if the export fails.

    Imports are deferred so the ``opentelemetry-exporter-otlp-proto-*``
    packages are only required when the ``"deep"`` validation level is used.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult

    raw_exporter: Any
    if protocol_type == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as OTLPGrpcSpanExporter,
        )

        # Per tracing/v2 interface spec, gRPC receiver URLs never contain a
        # scheme (e.g. "host:port"), meaning the connection is always plain/insecure.
        # See: https://github.com/canonical/charmlibs/tree/main/interfaces/tracing/interface/v2
        raw_exporter = OTLPGrpcSpanExporter(endpoint=url, timeout=timeout, insecure=True)
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as OTLPHttpSpanExporter,
        )

        endpoint = url.rstrip("/") + "/v1/traces"
        raw_exporter = OTLPHttpSpanExporter(endpoint=endpoint, timeout=timeout)

    capture = _ExportResultCapture(raw_exporter)
    resource = Resource.create({"service.name": "validators.tracing"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(capture))  # type: ignore[arg-type]
    tracer = provider.get_tracer("validators.tracing")

    with tracer.start_as_current_span("validator-probe"):
        pass

    provider.shutdown()

    if capture.result is None or capture.result != SpanExportResult.SUCCESS:
        if capture._exc is not None:
            raise RuntimeError(f"Span export to {url!r} failed: {capture._exc}")
        detail = capture._log or capture.result.name if capture.result is not None else "no export attempted"
        raise RuntimeError(f"Span export to {url!r} failed: {detail}.")


class _ExportResultCapture:
    """Thin exporter wrapper that records the most recent export result."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.result: Any = None
        self._exc: BaseException | None = None
        self._log: str | None = None

    def export(self, spans: Any) -> Any:
        import logging as _logging

        from opentelemetry.sdk.trace.export import SpanExportResult

        # The gRPC/HTTP exporters swallow exceptions and return FAILURE, but
        # they do log the underlying error. Capture that log message so we can
        # surface it to the caller.
        captured_log: list[str] = []

        class _LogCapture(_logging.Handler):
            def emit(self, record: _logging.LogRecord) -> None:
                captured_log.append(record.getMessage())

        handler = _LogCapture()
        otlp_logger = _logging.getLogger("opentelemetry.exporter.otlp")
        otlp_logger.addHandler(handler)
        try:
            self.result = self._inner.export(spans)
        except Exception as exc:
            self.result = SpanExportResult.FAILURE
            self._exc = exc
        finally:
            otlp_logger.removeHandler(handler)

        if captured_log:
            self._log = captured_log[-1]
        return self.result

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Low-level network helpers
# ---------------------------------------------------------------------------


def _parse_host_port(url: str, protocol_type: str) -> tuple[str, int]:
    """Return *(host, port)* from a receiver URL.

    gRPC URLs are often bare ``host:port`` without a scheme; a synthetic
    scheme is prepended so that :func:`urlparse` can handle them correctly.
    """
    if "://" not in url:
        url = f"grpc://{url}"
    parsed = urlparse(url)
    host = parsed.hostname or url
    if parsed.port is not None:
        return host, parsed.port
    # Fall back to well-known defaults
    if parsed.scheme in ("https", "grpcs"):
        return host, 443
    if protocol_type == "grpc":
        return host, 4317
    return host, 80


def _tcp_ping(host: str, port: int, timeout: float = 5.0) -> None:
    """Open a TCP connection to *host*:*port* and immediately close it."""
    with socket.create_connection((host, port), timeout=timeout):
        pass
