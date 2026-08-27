# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import socket
import ssl
import time
import uuid
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

_PUSH_PATH = "/loki/api/v1/push"


def _base_url(push_url: str) -> str:
    """Return the scheme+host(+prefix) portion of *push_url*, with the push path stripped."""
    parsed = urlparse(push_url)
    prefix = parsed.path[: -len(_PUSH_PATH)] if parsed.path.endswith(_PUSH_PATH) else ""
    return f"{parsed.scheme}://{parsed.netloc}{prefix}"


class LokiPushApiValidator(BaseValidator):
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
        if level == "deep" and all(c.passed for c in checks):
            checks.extend(_canary_checks(endpoint_infos))

        return self._make_result(level=level, checks=checks)


# ---------------------------------------------------------------------------
# Pure helpers — endpoint collection
# ---------------------------------------------------------------------------


def _collect_endpoint_infos(
    relation: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return deduplicated endpoint info dicts and any collection errors.

    The loki_push_api v1 provider writes::

        relation.data[unit]["endpoint"] = json.dumps({"url": "<push_url>"})

    for each provider unit.  Malformed entries (invalid JSON, non-dict,
    missing/non-string url) are not silently dropped — they are returned as
    errors so ``_schema_check`` can report every bad databag.
    """
    infos: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    for unit in sorted(relation.units, key=lambda u: getattr(u, "name", repr(u))):
        unit_name = getattr(unit, "name", repr(unit))
        raw = relation.data[unit].get("endpoint", "")
        if not raw:
            errors.append(f"Unit {unit_name!r}: no 'endpoint' key in databag")
            continue
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"Unit {unit_name!r}: 'endpoint' is not valid JSON: {exc}")
            continue
        if not isinstance(info, dict):
            errors.append(f"Unit {unit_name!r}: 'endpoint' must be a JSON object, got {type(info).__name__!r}")
            continue
        url = info.get("url")
        if url is None:
            errors.append(f"Unit {unit_name!r}: 'endpoint' dict has no 'url' field")
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

    * ``url`` is a string, http/https, has a hostname, and path ends with
      ``/loki/api/v1/push``.
    * ``labels`` (optional) — when present, must be a JSON-decodable dict.
    * ``ca_cert`` (optional, HTTPS only) — when present, must be a non-empty
      string (expected to be a PEM certificate).
    * ``tls_insecure_skip_verify`` (optional, HTTPS only) — when present, must
      be the string ``"true"`` or ``"false"``.

    Collection errors (malformed unit databags) are also included in the
    failure message so that every bad endpoint is surfaced in one check.
    """
    if not endpoint_infos and not collection_errors:
        return ValidationCheck(
            name="schema",
            passed=False,
            message="No 'endpoint' data found in provider unit databags.",
        )

    errors: list[str] = list(collection_errors)
    for info in endpoint_infos:
        url = info.get("url", "")
        # Guard: url must be a string even if _collect_endpoint_infos already
        # ensured this; validates defensive typing before calling urlparse.
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
        if not parsed.path.endswith(_PUSH_PATH):
            errors.append(f"{url!r}: path must end with {_PUSH_PATH!r}")

        # Optional: labels must be a dict if present
        labels_raw = info.get("labels")
        if labels_raw is not None:
            try:
                labels = json.loads(labels_raw) if isinstance(labels_raw, str) else labels_raw
                if not isinstance(labels, dict):
                    errors.append(f"{url!r}: 'labels' must be a JSON object, got {type(labels).__name__}")
            except json.JSONDecodeError as exc:
                errors.append(f"{url!r}: 'labels' is not valid JSON: {exc}")

        # Optional TLS fields (only meaningful for HTTPS)
        if parsed.scheme == "https":
            ca_cert = info.get("ca_cert")
            if ca_cert is not None and not (isinstance(ca_cert, str) and ca_cert.strip()):
                errors.append(f"{url!r}: 'ca_cert' must be a non-empty string when present")
            tls_skip = info.get("tls_insecure_skip_verify")
            if tls_skip is not None and tls_skip not in ("true", "false"):
                errors.append(f"{url!r}: 'tls_insecure_skip_verify' must be 'true' or 'false', got {tls_skip!r}")

    if errors:
        return ValidationCheck(name="schema", passed=False, message="; ".join(errors))
    return ValidationCheck(name="schema", passed=True, message=f"Validated {len(endpoint_infos)} endpoint(s).")


# ---------------------------------------------------------------------------
# Pure helpers — L1 connectivity & readiness
# ---------------------------------------------------------------------------


def _connectivity_check(endpoint_infos: list[dict[str, Any]]) -> ValidationCheck:
    """TCP-ping every Loki push endpoint; return a single pass/fail check."""
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
    """HTTP GET ``/ready`` for each push URL; confirm 200 OK + body "ready".

    Loki may return 503 for up to ~25 s while its ingester warms up, so the
    check retries with back-off before reporting failure (6 attempts, 5 s apart).

    Push-only forwarders (e.g. grafana-agent-k8s, opentelemetry-collector-k8s)
    don't implement ``/ready`` and 404 immediately. A bare 404 on ``/ready`` is
    not, by itself, proof that the provider is a working push-only forwarder —
    it's equally consistent with a fully broken/misconfigured endpoint that
    404s on every route. So on a 404, ``_probe_push_endpoint`` positively
    confirms the *actual advertised push route* is live (see its docstring)
    before this check is allowed to pass.
    """
    checks: list[ValidationCheck] = []
    for info in endpoint_infos:
        url: str = info["url"]
        parsed = urlparse(url)
        base = _base_url(url)
        ready_url = f"{base}/ready"
        check_name = f"http_ready[{parsed.netloc}]"
        last_msg = ""
        passed = False
        try:
            ssl_ctx = _build_ssl_context(info)
        except Exception as exc:
            checks.append(ValidationCheck(name=check_name, passed=False, message=f"TLS context error: {exc}"))
            continue
        for attempt in range(6):
            if attempt:
                time.sleep(5)
            try:
                with urlopen(ready_url, timeout=5, context=ssl_ctx) as resp:  # nosec B310
                    body = resp.read().decode("utf-8", errors="replace")
                    if resp.status == 200 and "ready" in body.lower():
                        passed = True
                        last_msg = f"Loki ready at {ready_url}."
                        break
                    last_msg = f"Unexpected response {resp.status} from {ready_url}: {body[:200]}"
            except HTTPError as exc:
                if exc.code == 404:
                    passed, last_msg = _probe_push_endpoint(url, ssl_ctx)
                    break
                last_msg = str(exc)
            except Exception as exc:
                last_msg = str(exc)
        checks.append(ValidationCheck(name=check_name, passed=passed, message=last_msg))
    return checks


def _probe_push_endpoint(url: str, ssl_ctx: ssl.SSLContext | None = None) -> tuple[bool, str]:
    """POST an empty-streams no-op payload to *url* to confirm the push route is live.

    Only called when ``/ready`` 404s. A 404 there is ambiguous: push-only
    forwarders (no ``/ready`` route) and fully broken/misconfigured endpoints
    (404 on *every* route) look identical from that single signal alone. This
    probe distinguishes them by exercising the actual advertised push route
    with a harmless payload:

    * ``{"streams": []}`` is empty, so a compliant push receiver accepts it
      (200/204) without writing any log data — confirmed live against both
      loki-k8s and opentelemetry-collector-k8s.
    * Any other 4xx (e.g. 400/405) still proves the route is registered and
      actively handled by the application, even though it rejected this
      specific probe.
    * A 404 here means the advertised push route itself doesn't exist — a
      real failure, not a push-only forwarder.
    * 5xx or a network-level error is a genuine backend problem.
    """
    payload = json.dumps({"streams": []}).encode()
    req = Request(url, data=payload, headers={"Content-Type": "application/json"})  # nosec B310
    try:
        with urlopen(req, timeout=5, context=ssl_ctx) as resp:  # nosec B310
            return True, (
                f"No '/ready' route at {url}, but push endpoint accepted an empty no-op push "
                f"(HTTP {resp.status}); provider does not expose a Loki-compatible readiness "
                "endpoint (push-only forwarder)."
            )
    except HTTPError as exc:
        if exc.code == 404:
            return False, f"Push endpoint {url} also returned 404; no working route found."
        if 500 <= exc.code < 600:
            return False, f"Push endpoint {url} returned server error: {exc}"
        return True, (
            f"No '/ready' route at {url}, but push endpoint is registered (HTTP {exc.code} {exc.reason}); "
            "provider does not expose a Loki-compatible readiness endpoint (push-only forwarder)."
        )
    except Exception as exc:
        return False, f"Error probing push endpoint {url}: {exc}"


# ---------------------------------------------------------------------------
# Pure helpers — L2 canary write + query round-trip
# ---------------------------------------------------------------------------

_CANARY_LABEL_KEY = "__validator_probe__"
_CANARY_JOB = "validators-loki-push-api"
# Wait for Loki to flush the ingested stream before querying.
_INGEST_WAIT_S = 3
# Bounded lookback window for the read-back query. Kept short (well under
# typical Loki max-query-length limits) since the canary log was just pushed.
_QUERY_WINDOW_S = 3600


def _canary_checks(endpoint_infos: list[dict[str, Any]]) -> list[ValidationCheck]:
    """Push a canary log entry to each endpoint then, if possible, query it back.

    The loki_push_api interface only guarantees a push endpoint — some
    providers (e.g. grafana-agent-k8s) act as push-only forwarders and don't
    expose any query API at all. Read-back verification is therefore
    best-effort: query capability is detected generically (not by charm name)
    via ``_supports_query_api`` before attempting the round trip. When no
    query API is available, the check passes based on the push alone.

    Uses a unique per-run label value so concurrent runs don't cross-pollinate.
    Returns one check per URL covering the push, and the query when supported.
    """
    checks: list[ValidationCheck] = []
    for info in endpoint_infos:
        url: str = info["url"]
        probe_id = uuid.uuid4().hex[:12]
        check_name = f"canary[{urlparse(url).netloc}]"
        try:
            ssl_ctx = _build_ssl_context(info)
        except Exception as exc:
            checks.append(ValidationCheck(name=check_name, passed=False, message=f"TLS context error: {exc}"))
            continue
        try:
            _push_canary(url, probe_id, ssl_ctx)
        except Exception as exc:
            checks.append(ValidationCheck(name=check_name, passed=False, message=f"Push failed: {exc}"))
            continue

        base = _base_url(url)
        try:
            query_capable = _supports_query_api(base, ssl_ctx)
        except Exception as exc:
            checks.append(
                ValidationCheck(name=check_name, passed=False, message=f"Query capability probe failed: {exc}")
            )
            continue

        if not query_capable:
            checks.append(
                ValidationCheck(
                    name=check_name,
                    passed=True,
                    message=(
                        f"Canary log pushed to {url}. Provider does not expose a Loki query API "
                        "(push-only forwarder); skipping read-back verification."
                    ),
                )
            )
            continue

        time.sleep(_INGEST_WAIT_S)

        try:
            found = _query_canary(url, probe_id, ssl_ctx)
        except Exception as exc:
            checks.append(ValidationCheck(name=check_name, passed=False, message=f"Query failed: {exc}"))
            continue

        if found:
            checks.append(
                ValidationCheck(
                    name=check_name, passed=True, message=f"Canary log written and queried back from {url}."
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    name=check_name,
                    passed=False,
                    message=f"Canary log pushed to {url} but not found in query results.",
                )
            )
    return checks


def _push_canary(url: str, probe_id: str, ssl_ctx: ssl.SSLContext | None = None) -> None:
    """POST a single log line with a unique probe label to *url*."""
    ts_ns = str(time.time_ns())
    payload = json.dumps(
        {
            "streams": [
                {
                    "stream": {_CANARY_LABEL_KEY: probe_id, "job": _CANARY_JOB},
                    "values": [[ts_ns, f"validator probe {probe_id}"]],
                }
            ]
        }
    ).encode()
    req = Request(url, data=payload, headers={"Content-Type": "application/json"})  # nosec B310
    with urlopen(req, timeout=10, context=ssl_ctx) as resp:  # nosec B310
        if resp.status not in (200, 204):
            raise RuntimeError(f"Unexpected push response: HTTP {resp.status}")


def _supports_query_api(base: str, ssl_ctx: ssl.SSLContext | None = None) -> bool:
    """Return True if *base* exposes a Loki query API.

    Push-only forwarders (e.g. grafana-agent-k8s) return 404 for every
    ``/loki/api/v1/query*`` route. Probe a lightweight, side-effect-free
    endpoint (``/loki/api/v1/labels``) rather than assuming which provider
    implementations do or don't support querying.
    """
    labels_url = f"{base}/loki/api/v1/labels"
    try:
        with urlopen(labels_url, timeout=10, context=ssl_ctx) as resp:  # nosec B310
            return bool(resp.status == 200)
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def _query_canary(push_url: str, probe_id: str, ssl_ctx: ssl.SSLContext | None = None) -> bool:
    """Query Loki for the canary log line; return True if found.

    Uses ``/loki/api/v1/query_range`` (not ``/loki/api/v1/query``): the latter
    is an *instant query* endpoint that Loki rejects for log selectors ("log
    queries are not supported as an instant query type"), whereas
    ``query_range`` is the correct endpoint for retrieving log lines over a
    bounded time window.
    """
    base = _base_url(push_url)
    now_ns = time.time_ns()
    start_ns = now_ns - _QUERY_WINDOW_S * 1_000_000_000
    logql = f'{{{_CANARY_LABEL_KEY}="{probe_id}"}}'
    query_url = f"{base}/loki/api/v1/query_range?" + urlencode(
        {
            "query": logql,
            "limit": "1",
            "direction": "BACKWARD",
            "start": str(start_ns),
            "end": str(now_ns),
        }
    )
    with urlopen(query_url, timeout=10, context=ssl_ctx) as resp:  # nosec B310
        body = json.loads(resp.read())
    result_type = body.get("data", {}).get("resultType", "")
    streams = body.get("data", {}).get("result", [])
    if result_type != "streams":
        raise RuntimeError(f"Unexpected query resultType: {result_type!r}")
    return bool(streams)


# ---------------------------------------------------------------------------
# TLS helper
# ---------------------------------------------------------------------------


def _build_ssl_context(info: dict[str, Any]) -> ssl.SSLContext | None:
    """Build an SSL context from TLS fields in the endpoint info dict.

    Returns ``None`` for HTTP endpoints or HTTPS endpoints with no special TLS
    configuration (urllib then uses the system default context).

    * ``tls_insecure_skip_verify = "true"`` — disables hostname and certificate
      verification (useful for self-signed certs in test environments).
    * ``ca_cert`` — a PEM-encoded CA certificate used to verify the server
      certificate (takes precedence over the system CA bundle).
    """
    url: str = info.get("url", "")
    if urlparse(url).scheme != "https":
        return None

    ca_cert: str | None = info.get("ca_cert")
    tls_skip: str | None = info.get("tls_insecure_skip_verify")

    if tls_skip == "true":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    if ca_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cadata=ca_cert)
        return ctx

    return None


# ---------------------------------------------------------------------------
# Low-level network helpers
# ---------------------------------------------------------------------------


def _tcp_ping(host: str, port: int, timeout: float = 5.0) -> None:
    """Open a TCP connection to *host*:*port* and immediately close it."""
    with socket.create_connection((host, port), timeout=timeout):
        pass
