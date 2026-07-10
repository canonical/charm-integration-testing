# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import socket
import struct
import time
import urllib.error
import uuid
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

_REMOTE_WRITE_PATH = "/api/v1/write"
_READY_PATH = "/-/ready"
_CANARY_METRIC = "validator_canary"
_CANARY_LABEL_KEY = "__validator_probe__"
_INGEST_WAIT_S = 3


class PrometheusRemoteWriteValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if self.relation.app is None:
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        endpoint_infos, collection_errors = _collect_endpoint_infos(self.relation)

        schema_check = _schema_check(endpoint_infos, collection_errors)
        checks: list[ValidationCheck] = [schema_check]
        if not schema_check.passed:
            return self._fail_result(level, checks)

        checks.append(_connectivity_check(endpoint_infos))
        if not checks[-1].passed:
            return self._fail_result(level, checks)

        checks.extend(_http_ready_checks(endpoint_infos))
        if not all(c.passed for c in checks):
            return self._fail_result(level, checks)

        if level == "deep":
            checks.extend(_canary_checks(endpoint_infos))

        return self._make_result(level=level, checks=checks)


# ---------------------------------------------------------------------------
# Pure helpers — endpoint collection from unit databags
# ---------------------------------------------------------------------------


def _collect_endpoint_infos(
    relation: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return deduplicated endpoint info dicts and any collection errors.

    The prometheus_remote_write v1 provider writes::

        relation.data[unit]["remote_write"] = json.dumps({"url": "<write_url>"})

    for each provider unit.  Malformed entries are returned as errors so that
    ``_schema_check`` can surface every bad databag in one check.
    """
    infos: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    for unit in sorted(relation.units, key=lambda u: getattr(u, "name", repr(u))):
        unit_name = getattr(unit, "name", repr(unit))
        raw = relation.data[unit].get("remote_write", "")
        if not raw:
            errors.append(f"Unit {unit_name!r}: no 'remote_write' key in databag")
            continue
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"Unit {unit_name!r}: 'remote_write' is not valid JSON: {exc}")
            continue
        if not isinstance(info, dict):
            errors.append(f"Unit {unit_name!r}: 'remote_write' must be a JSON object, got {type(info).__name__!r}")
            continue
        url = info.get("url")
        if url is None:
            errors.append(f"Unit {unit_name!r}: 'remote_write' dict has no 'url' field")
            continue
        if not isinstance(url, str):
            errors.append(f"Unit {unit_name!r}: 'url' must be a string, got {type(url).__name__!r}")
            continue
        if not url:
            errors.append(f"Unit {unit_name!r}: 'url' is empty")
            continue
        if url in seen_urls:
            continue  # dedup — same URL from a scaled-out provider is not an error
        seen_urls.add(url)
        infos.append(info)
    return infos, errors


# ---------------------------------------------------------------------------
# Pure helpers — schema
# ---------------------------------------------------------------------------


def _schema_check(
    endpoint_infos: list[dict[str, Any]],
    collection_errors: list[str],
) -> ValidationCheck:
    """Validate structure and field types of all advertised endpoint dicts.

    Checks applied per endpoint:

    * ``url`` is a string, http/https scheme, has a hostname, and path ends
      with ``/api/v1/write``.
    """
    if not endpoint_infos and not collection_errors:
        return ValidationCheck(
            name="schema",
            passed=False,
            message="No 'remote_write' data found in provider unit databags.",
        )

    errors: list[str] = list(collection_errors)
    for info in endpoint_infos:
        url = info.get("url", "")
        if not isinstance(url, str):
            errors.append(f"'url' must be a string, got {type(url).__name__!r}")
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            errors.append(f"{url!r}: unsupported scheme {parsed.scheme!r}")
            continue
        if not parsed.hostname:
            errors.append(f"{url!r}: missing hostname")
            continue
        if not parsed.path.endswith(_REMOTE_WRITE_PATH):
            errors.append(f"{url!r}: path must end with {_REMOTE_WRITE_PATH!r}")

    if errors:
        return ValidationCheck(name="schema", passed=False, message="; ".join(errors))
    return ValidationCheck(name="schema", passed=True, message=f"Validated {len(endpoint_infos)} endpoint(s).")


# ---------------------------------------------------------------------------
# Pure helpers — L1 connectivity & readiness
# ---------------------------------------------------------------------------


def _connectivity_check(endpoint_infos: list[dict[str, Any]]) -> ValidationCheck:
    """TCP-ping every remote write endpoint; return a single pass/fail check."""
    errors: list[str] = []
    for info in endpoint_infos:
        url: str = info["url"]
        try:
            parsed = urlparse(url)
            host = parsed.hostname or url
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            _tcp_ping(host, port)
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    if errors:
        return ValidationCheck(name="connect", passed=False, message="; ".join(errors))
    return ValidationCheck(name="connect", passed=True, message=f"TCP reached {len(endpoint_infos)} endpoint(s).")


def _http_ready_checks(endpoint_infos: list[dict[str, Any]]) -> list[ValidationCheck]:
    """HTTP GET ``/-/ready`` for each remote write URL; confirm 200 OK.

    Prometheus may take a few seconds to become ready after startup, so the
    check retries with back-off before reporting failure (3 attempts, 3 s apart).
    """
    checks: list[ValidationCheck] = []
    for info in endpoint_infos:
        url: str = info["url"]
        parsed = urlparse(url)
        path_prefix = parsed.path[: -len(_REMOTE_WRITE_PATH)]
        base = f"{parsed.scheme}://{parsed.netloc}{path_prefix}"
        ready_url = f"{base}{_READY_PATH}"
        check_name = f"http_ready[{parsed.netloc}]"
        last_msg = ""
        passed = False
        for attempt in range(3):
            if attempt:
                time.sleep(3)
            try:
                with urlopen(ready_url, timeout=5) as resp:  # nosec B310
                    resp.read()
                    passed = True
                    last_msg = f"Prometheus ready at {ready_url}."
                    break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                last_msg = f"Unexpected response {exc.code} from {ready_url}: {body[:200]}"
            except Exception as exc:
                last_msg = str(exc)
        checks.append(ValidationCheck(name=check_name, passed=passed, message=last_msg))
    return checks


# ---------------------------------------------------------------------------
# Pure helpers — L2 canary write + query round-trip
# ---------------------------------------------------------------------------


def _canary_checks(endpoint_infos: list[dict[str, Any]]) -> list[ValidationCheck]:
    """Push a canary metric to each remote_write endpoint then query it back.

    Uses a unique per-run probe label so concurrent validator runs do not
    cross-pollinate.  Returns one check per endpoint covering both the push
    and the query.

    Protocol: the Prometheus remote_write v1 wire format is a snappy-compressed
    protobuf ``WriteRequest`` message.  Both are encoded inline below to keep the
    validator dependency-free.
    """
    checks: list[ValidationCheck] = []
    for info in endpoint_infos:
        url: str = info["url"]
        parsed = urlparse(url)
        path_prefix = parsed.path[: -len(_REMOTE_WRITE_PATH)]
        base_url = f"{parsed.scheme}://{parsed.netloc}{path_prefix}"
        check_name = f"canary[{parsed.netloc}]"
        probe_id = uuid.uuid4().hex[:12]
        timestamp_ms = int(time.time() * 1000)

        try:
            _push_canary(url, probe_id, timestamp_ms)
        except Exception as exc:
            checks.append(ValidationCheck(name=check_name, passed=False, message=f"Push failed: {exc}"))
            continue

        time.sleep(_INGEST_WAIT_S)

        try:
            found = _query_canary(base_url, probe_id)
        except Exception as exc:
            checks.append(ValidationCheck(name=check_name, passed=False, message=f"Query failed: {exc}"))
            continue

        if found:
            checks.append(
                ValidationCheck(
                    name=check_name,
                    passed=True,
                    message=f"Canary metric written and queried back from {parsed.netloc}.",
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    name=check_name,
                    passed=False,
                    message=f"Canary metric pushed to {parsed.netloc} but not found in query results.",
                )
            )
    return checks


def _push_canary(url: str, probe_id: str, timestamp_ms: int) -> None:
    """POST a single canary metric to *url* using the Prometheus remote_write v1 protocol."""
    payload = _snappy_compress(
        _build_write_request(
            metric_name=_CANARY_METRIC,
            probe_id=probe_id,
            value=1.0,
            timestamp_ms=timestamp_ms,
        )
    )
    req = Request(url, data=payload, method="POST")  # nosec B310
    req.add_header("Content-Type", "application/x-protobuf")
    req.add_header("Content-Encoding", "snappy")
    req.add_header("X-Prometheus-Remote-Write-Version", "0.1.0")
    try:
        with urlopen(req, timeout=10) as resp:  # nosec B310
            if resp.status not in (200, 204):
                raise RuntimeError(f"Unexpected push response: HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Push rejected: HTTP {exc.code}") from exc


def _query_canary(base_url: str, probe_id: str) -> bool:
    """Query Prometheus for the canary metric; return True if found."""
    query = f'{_CANARY_METRIC}{{{_CANARY_LABEL_KEY}="{probe_id}"}}'
    query_url = f"{base_url}/api/v1/query?query={quote(query)}"
    with urlopen(query_url, timeout=10) as resp:  # nosec B310
        body = json.loads(resp.read())
    status = body.get("status")
    if status != "success":
        raise RuntimeError(f"Prometheus query returned status {status!r}: {body.get('error', '')}")
    result = body.get("data", {}).get("result", [])
    return bool(result)


# ---------------------------------------------------------------------------
# Pure helpers — Prometheus remote_write v1 wire encoding
# ---------------------------------------------------------------------------
# The remote_write v1 protocol sends a snappy-compressed protobuf WriteRequest.
# Both encoders are implemented inline to avoid adding binary dependencies to
# the validator package.


def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf unsigned varint."""
    pieces = bytearray()
    while value > 0x7F:
        pieces.append((value & 0x7F) | 0x80)
        value >>= 7
    pieces.append(value & 0x7F)
    return bytes(pieces)


def _encode_ld(field: int, data: bytes) -> bytes:
    """Encode a length-delimited protobuf field (wire type 2)."""
    tag = (field << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(data)) + data


def _encode_str(field: int, s: str) -> bytes:
    """Encode a UTF-8 string as a protobuf length-delimited field."""
    return _encode_ld(field, s.encode())


def _encode_double(field: int, v: float) -> bytes:
    """Encode a double as a protobuf 64-bit fixed field (wire type 1)."""
    tag = (field << 3) | 1
    return _encode_varint(tag) + struct.pack("<d", v)


def _encode_int64(field: int, v: int) -> bytes:
    """Encode a signed int64 as a protobuf varint field (wire type 0)."""
    if v < 0:
        v += 1 << 64
    tag = field << 3
    return _encode_varint(tag) + _encode_varint(v)


def _build_write_request(metric_name: str, probe_id: str, value: float, timestamp_ms: int) -> bytes:
    """Build a minimal prometheus WriteRequest protobuf for a single time-series.

    Schema (prometheus/prompb/types.proto v1):
        WriteRequest  { repeated TimeSeries timeseries = 1; }
        TimeSeries    { repeated Label labels = 1; repeated Sample samples = 2; }
        Label         { string name = 1; string value = 2; }
        Sample        { double value = 1; int64 timestamp = 2; }
    """
    name_label = _encode_str(1, "__name__") + _encode_str(2, metric_name)
    probe_label = _encode_str(1, _CANARY_LABEL_KEY) + _encode_str(2, probe_id)
    sample = _encode_double(1, value) + _encode_int64(2, timestamp_ms)
    timeseries = _encode_ld(1, name_label) + _encode_ld(1, probe_label) + _encode_ld(2, sample)
    return _encode_ld(1, timeseries)


def _snappy_compress(data: bytes) -> bytes:
    """Encode *data* in the Snappy raw (non-framed) format using literal-only blocks.

    The format is: varint(uncompressed_length) followed by a sequence of
    element chunks.  A literal element with length L (1 ≤ L ≤ 60) is encoded
    as a single tag byte ``(L-1) << 2`` followed by the L raw bytes.  Using
    literal-only blocks produces valid – if uncompressed – Snappy output that
    Prometheus accepts on the remote_write endpoint.
    """
    result = bytearray(_encode_varint(len(data)))
    i = 0
    while i < len(data):
        chunk = data[i : i + 60]
        result.append((len(chunk) - 1) << 2)
        result.extend(chunk)
        i += 60
    return bytes(result)


# ---------------------------------------------------------------------------
# Low-level network helpers
# ---------------------------------------------------------------------------


def _tcp_ping(host: str, port: int, timeout: float = 5.0) -> None:
    """Open a TCP connection to *host*:*port* and immediately close it."""
    with socket.create_connection((host, port), timeout=timeout):
        pass
