# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import socket
import time
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

_HEALTHY_PATH = "/-/healthy"
_ALERTS_PATH = "/api/v2/alerts"
_SILENCES_PATH = "/api/v2/silences"
# A single silence is fetched/deleted via the singular path per the Alertmanager API v2 spec.
_SILENCE_PATH = "/api/v2/silence"
_CANARY_ALERTNAME = "EndpointValidatorCanary"
_CANARY_LABEL_KEY = "validator_probe"
_VALIDATOR_ID = "alertmanager_dispatch-validator"
_INGEST_WAIT_S = 2
_QUERY_ATTEMPTS = 3
_HEALTH_ATTEMPTS = 3
_HEALTH_BACKOFF_S = 3
_HEALTH_TIMEOUT_S = 5
_CONNECT_TIMEOUT_S = 5
_ALERTS_TIMEOUT_S = 10
_CANARY_ACTIVE_MIN = 5
_SILENCE_DURATION_MIN = 10
_CLOCK_SKEW_S = 30
_SILENCE_SETTLE_ATTEMPTS = 5
_SILENCE_SETTLE_BACKOFF_S = 1
_RESOLVE_CONFIRM_ATTEMPTS = 3
_RESOLVE_CONFIRM_BACKOFF_S = 2
_PROBE_ID_LEN = 12


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse HTTP redirects so a login-page 302 cannot be followed to a 200 and read as healthy."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


# Reject redirects on every call: an auth proxy bouncing /-/healthy to a login page must surface
# as an error, not a masked 200. A rejected redirect raises HTTPError, which each caller handles.
# ProxyHandler({}) ignores http_proxy/https_proxy so in-model endpoints are reached directly rather
# than routed through (or blocked by) a CI/dev corporate proxy.
urlopen = build_opener(ProxyHandler({}), _NoRedirectHandler).open


class AlertmanagerDispatchValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if self.relation.app is None:
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        urls, collection_errors = _collect_endpoint_urls(self.relation)

        schema_check = _schema_check(urls, collection_errors)
        checks: list[ValidationCheck] = [schema_check]
        if not schema_check.passed:
            return self._fail_result(level, checks)

        checks.append(_connectivity_check(urls))
        if not checks[-1].passed:
            return self._fail_result(level, checks)

        checks.extend(_http_healthy_checks(urls))
        if level == "deep" and all(c.passed for c in checks):
            checks.extend(_canary_checks(urls))

        return self._make_result(level=level, checks=checks)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _base_url(url: str) -> str:
    """Return *url* without a trailing slash so API paths can be appended."""
    return url.rstrip("/")


def _silence_url(base_url: str, silence_id: str) -> str:
    """Return the singular single-silence URL used to GET or DELETE one silence."""
    return f"{base_url}{_SILENCE_PATH}/{quote(silence_id, safe='')}"


def _display_netloc(parsed: Any) -> str:
    """Return ``host[:port]`` with any ``user:pass@`` userinfo stripped so credentials never reach reports."""
    netloc: str = parsed.netloc
    at = netloc.rfind("@")
    return netloc[at + 1 :] if at != -1 else netloc


def _redact_url(url: str) -> str:
    """Return *url* stripped of ``user:pass@`` userinfo and any ``?``/``#`` query/fragment for display.

    Uses string ops rather than :func:`urlparse` so secrets are removed even from a URL that is
    malformed enough for ``urlparse`` to reject (e.g. an unmatched IPv6 bracket) or that omits the
    ``://`` separator entirely (e.g. a bare ``user:pass@host`` value that lands in an error message).
    Schema validation rejects both userinfo and query/fragment, so this only guards display strings.
    """
    scheme_sep = url.find("://")
    # Without a scheme separator the whole value up to the first '/', '?' or '#' is the authority.
    authority_start = scheme_sep + len("://") if scheme_sep != -1 else 0
    authority_end = len(url)
    for i in range(authority_start, len(url)):
        if url[i] in "/?#":
            authority_end = i
            break
    authority = url[authority_start:authority_end]
    at = authority.rfind("@")
    if at != -1:
        url = url[:authority_start] + authority[at + 1 :] + url[authority_end:]
    # Drop query/fragment: they can carry secrets such as '?token=...'.
    for i, ch in enumerate(url):
        if ch in "?#":
            return url[:i]
    return url


def _http_error_body(exc: urllib.error.HTTPError) -> str:
    """Best-effort decode of an HTTPError response body for diagnostics."""
    return exc.read().decode("utf-8", errors="replace") if exc.fp else ""


def _http_error(exc: urllib.error.HTTPError, action: str) -> RuntimeError:
    """Wrap an HTTPError as a RuntimeError carrying a truncated response body."""
    return RuntimeError(f"{action}: HTTP {exc.code}: {_http_error_body(exc)[:200]}")


# ---------------------------------------------------------------------------
# Pure helpers — endpoint collection from provider unit databags
# ---------------------------------------------------------------------------


def _collect_endpoint_urls(
    relation: Any,
) -> tuple[list[str], list[str]]:
    """Return deduplicated endpoint URLs and any collection errors.

    The ``alertmanager_dispatch`` provider (Alertmanager) writes its workload
    URL to each of its **unit** databags. Two on-wire shapes exist:

    * v1: ``relation.data[unit]["url"] = "<scheme>://<host>:<port>"``
    * v0: ``relation.data[unit]["public_address"] = "<host>:<port>"`` with an
      optional ``relation.data[unit]["scheme"]`` that defaults to ``http`` only
      when the key is absent (an explicitly empty scheme is malformed and is
      left to fail the schema check).

    Malformed entries are returned as errors so ``_schema_check`` can surface
    every bad databag in one check.
    """
    urls: list[str] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    for unit in sorted(relation.units, key=lambda u: getattr(u, "name", repr(u))):
        unit_name = getattr(unit, "name", repr(unit))
        data = relation.data[unit]

        url = data.get("url", "")
        if not url:
            public_address = data.get("public_address", "")
            if public_address:
                scheme = data.get("scheme", "http")
                url = f"{scheme}://{public_address}"

        if not url:
            errors.append(f"Unit {unit_name!r}: no 'url' or 'public_address' key in databag")
            continue
        if url in seen_urls:
            continue  # dedup — same URL from a scaled-out provider is not an error
        seen_urls.add(url)

        urls.append(url)
    return urls, errors


# ---------------------------------------------------------------------------
# Pure helpers — schema
# ---------------------------------------------------------------------------


def _schema_check(
    urls: list[str],
    collection_errors: list[str],
) -> ValidationCheck:
    """Validate structure and value constraints of every advertised endpoint.

    Each URL must use an http/https scheme and have a hostname. A port is optional
    (v1 permits portless ingress URLs; connectivity defaults to 80/443 by scheme).
    """
    if not urls and not collection_errors:
        return ValidationCheck(
            name="schema",
            passed=False,
            message="No alertmanager_dispatch data found in provider unit databags.",
        )

    errors: list[str] = list(collection_errors)
    for url in urls:
        try:
            parsed = urlparse(url)
            # Access .hostname/.port so a malformed URL (bad port, unmatched IPv6 bracket) fails here.
            scheme, hostname = parsed.scheme, parsed.hostname
            _ = parsed.port
        except ValueError as exc:
            errors.append(f"{_redact_url(url)!r}: malformed URL: {exc}")
            continue
        if scheme not in ("http", "https"):
            errors.append(f"{_redact_url(url)!r}: unsupported scheme {scheme!r}")
            continue
        if not hostname:
            errors.append(f"{_redact_url(url)!r}: missing hostname")
            continue
        # This interface has no credential contract; urllib would not honour userinfo anyway.
        if parsed.username is not None or parsed.password is not None:
            errors.append(f"{_redact_url(url)!r}: unexpected userinfo (credentials) in URL")
            continue
        # Reject raw '?'/'#' too: urlparse drops empty delimiters, and appended API paths would misroute.
        if "?" in url or "#" in url:
            errors.append(f"{_redact_url(url)!r}: unexpected query or fragment component")
            continue

    if errors:
        return ValidationCheck(name="schema", passed=False, message="; ".join(errors))
    return ValidationCheck(name="schema", passed=True, message=f"Validated {len(urls)} endpoint(s).")


# ---------------------------------------------------------------------------
# Pure helpers — L1 connectivity & health
# ---------------------------------------------------------------------------


def _tcp_ping(host: str, port: int, timeout: float = _CONNECT_TIMEOUT_S) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


def _connectivity_check(urls: list[str]) -> ValidationCheck:
    """TCP-ping every Alertmanager endpoint; return a single pass/fail check."""
    errors: list[str] = []
    for url in urls:
        try:
            parsed = urlparse(url)
            host = parsed.hostname or url
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            _tcp_ping(host, port)
        except Exception as exc:
            errors.append(f"{_redact_url(url)}: {exc}")

    if errors:
        return ValidationCheck(name="connect", passed=False, message="; ".join(errors))
    return ValidationCheck(name="connect", passed=True, message=f"TCP reached {len(urls)} endpoint(s).")


def _http_healthy_checks(urls: list[str]) -> list[ValidationCheck]:
    """HTTP GET ``/-/healthy`` for each Alertmanager URL; confirm 200 OK.

    Alertmanager may take a few seconds to become healthy after startup, so the
    check retries with back-off before reporting failure.
    """
    checks: list[ValidationCheck] = []
    for url in urls:
        parsed = urlparse(url)
        healthy_url = f"{_base_url(url)}{_HEALTHY_PATH}"
        display_url = _redact_url(healthy_url)  # defensive: never echo a raw URL into a report
        check_name = f"http_healthy[{_display_netloc(parsed)}]"
        last_msg = ""
        passed = False
        for attempt in range(_HEALTH_ATTEMPTS):
            if attempt:
                time.sleep(_HEALTH_BACKOFF_S)
            try:
                with urlopen(healthy_url, timeout=_HEALTH_TIMEOUT_S) as resp:  # nosec B310
                    resp.read()
                    if resp.status == 200:
                        passed = True
                        last_msg = f"Alertmanager healthy at {display_url}."
                        break
                    last_msg = f"Unexpected status {resp.status} from {display_url}."
            except urllib.error.HTTPError as exc:
                body = _http_error_body(exc)
                last_msg = f"Unexpected response {exc.code} from {display_url}: {body[:200]}"
            except Exception as exc:
                last_msg = str(exc)
        checks.append(ValidationCheck(name=check_name, passed=passed, message=last_msg))
    return checks


# ---------------------------------------------------------------------------
# Pure helpers — L2 canary dispatch round-trip
# ---------------------------------------------------------------------------


def _canary_checks(urls: list[str]) -> list[ValidationCheck]:
    """Silence, dispatch, read back, then resolve a canary alert per endpoint.

    Uses a unique per-run probe label so concurrent validator runs do not
    cross-pollinate. Returns two checks per endpoint: the push + query
    round-trip, and a cleanup check that fails if the canary could not be
    resolved or the silence could not be removed (leaving stray state).
    """
    checks: list[ValidationCheck] = []
    for url in urls:
        parsed = urlparse(url)
        base = _base_url(url)
        netloc = _display_netloc(parsed)  # host:port only — never leak userinfo into check names/messages
        check_name = f"canary[{netloc}]"
        cleanup_name = f"canary_cleanup[{netloc}]"
        probe_id = uuid.uuid4().hex[:_PROBE_ID_LEN]

        # Silence the probe label before dispatch so a catch-all or short
        # group_wait route cannot page a real receiver with the canary — a
        # delivered notification cannot be retracted by cleanup.
        try:
            silence_id = _create_silence(base, probe_id)
        except Exception as exc:
            checks.append(
                ValidationCheck(
                    name=check_name,
                    passed=False,
                    message=f"Failed to silence canary probe before dispatch on {netloc}: {exc}",
                )
            )
            continue  # never dispatch an unsilenced canary that could notify real receivers

        # A silence ID only means the silence was accepted, not that it has settled to the
        # 'active' state; dispatching against a pending silence could still page a real receiver.
        try:
            _confirm_silence_active(base, silence_id)
        except Exception as exc:
            checks.append(
                ValidationCheck(
                    name=check_name,
                    passed=False,
                    message=f"Silence not confirmed active before dispatch on {netloc}: {exc}",
                )
            )
            # No canary was dispatched, so remove the accepted silence now instead of leaving stray state.
            checks.append(_remove_silence_check(base, silence_id, cleanup_name, netloc))
            continue

        try:
            checks.append(_round_trip_check(base, probe_id, check_name, netloc))
        finally:
            # A dispatch that raised locally may still have reached Alertmanager,
            # so always resolve the canary and remove the silence.
            checks.append(_cleanup_check(base, probe_id, silence_id, cleanup_name, netloc))
    return checks


def _round_trip_check(base_url: str, probe_id: str, check_name: str, netloc: str) -> ValidationCheck:
    """Dispatch the canary and confirm it can be read back from the alerts API."""
    try:
        _push_canary(base_url, probe_id)
    except Exception as exc:
        return ValidationCheck(name=check_name, passed=False, message=f"Dispatch failed: {exc}")

    found = False
    query_error = ""
    for attempt in range(_QUERY_ATTEMPTS):
        # Query first, then wait only between retries: a canary that lands right away skips the ingest wait.
        if attempt:
            time.sleep(_INGEST_WAIT_S)
        try:
            found = _query_canary(base_url, probe_id)
        except Exception as exc:
            query_error = str(exc)
            continue
        query_error = ""  # a successful query supersedes any earlier transient error
        if found:
            break

    if found:
        return ValidationCheck(
            name=check_name, passed=True, message=f"Canary alert dispatched and read back from {netloc}."
        )
    if query_error:
        return ValidationCheck(name=check_name, passed=False, message=f"Query failed: {query_error}")
    return ValidationCheck(
        name=check_name, passed=False, message=f"Canary alert dispatched to {netloc} but not found in active alerts."
    )


def _cleanup_check(base_url: str, probe_id: str, silence_id: str, check_name: str, netloc: str) -> ValidationCheck:
    """Resolve the canary and remove its silence, reporting either failure.

    Unlike a silently swallowed error, a failed resolve (which leaves the alert
    active) or a failed silence removal must fail deep validation so stray state
    is surfaced.
    """
    try:
        _resolve_canary(base_url, probe_id)
    except Exception as exc:
        # Keep the silence until it expires; deleting it would leave an unresolved canary able to page.
        return ValidationCheck(
            name=check_name,
            passed=False,
            message=f"Canary cleanup failed on {netloc}; canary resolve failed: {exc}. "
            "Silence retained until expiry so the canary stays muted.",
        )
    # A resolve POST only sets endsAt; clock skew / HA propagation can keep the canary
    # active briefly, so confirm it is gone before removing the silence that mutes it.
    try:
        cleared = _confirm_canary_cleared(base_url, probe_id)
    except Exception as exc:
        return ValidationCheck(
            name=check_name,
            passed=False,
            message=f"Canary cleanup failed on {netloc}; could not confirm canary resolved: {exc}. "
            "Silence retained until expiry so the canary stays muted.",
        )
    if not cleared:
        return ValidationCheck(
            name=check_name,
            passed=False,
            message=f"Canary cleanup failed on {netloc}; canary still active after resolve. "
            "Silence retained until expiry so the canary stays muted.",
        )
    try:
        _delete_silence(base_url, silence_id)
    except Exception as exc:
        return ValidationCheck(
            name=check_name,
            passed=False,
            message=f"Canary cleanup failed on {netloc}; silence removal failed: {exc}",
        )
    return ValidationCheck(name=check_name, passed=True, message=f"Canary resolved and silence removed on {netloc}.")


def _remove_silence_check(base_url: str, silence_id: str, check_name: str, netloc: str) -> ValidationCheck:
    """Delete a silence created before any canary dispatch, surfacing failure as stray state."""
    try:
        _delete_silence(base_url, silence_id)
    except Exception as exc:
        return ValidationCheck(
            name=check_name,
            passed=False,
            message=f"Canary cleanup failed on {netloc}; silence removal failed: {exc}",
        )
    return ValidationCheck(name=check_name, passed=True, message=f"Silence removed on {netloc}.")


def _canary_payload(probe_id: str, starts_at: datetime, ends_at: datetime) -> bytes:
    alert = [
        {
            "labels": {
                "alertname": _CANARY_ALERTNAME,
                _CANARY_LABEL_KEY: probe_id,
                "severity": "info",
            },
            "annotations": {"summary": "canary alert emitted by the alertmanager_dispatch validator"},
            "startsAt": starts_at.isoformat(),
            "endsAt": ends_at.isoformat(),
        }
    ]
    return json.dumps(alert).encode("utf-8")


def _post_alerts(base_url: str, payload: bytes) -> None:
    req = Request(f"{base_url}{_ALERTS_PATH}", data=payload, method="POST")  # nosec B310
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=_ALERTS_TIMEOUT_S) as resp:  # nosec B310
            resp.read()
            if resp.status not in (200, 204):
                raise RuntimeError(f"Unexpected dispatch response: HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, "Dispatch rejected") from exc


def _push_canary(base_url: str, probe_id: str) -> None:
    """POST a single canary alert to *base_url*, active for a few minutes."""
    now = datetime.now(timezone.utc)
    _post_alerts(base_url, _canary_payload(probe_id, now, now + timedelta(minutes=_CANARY_ACTIVE_MIN)))


def _query_canary(base_url: str, probe_id: str) -> bool:
    """Query the active alerts endpoint; return True if the canary is present.

    A server-side ``filter`` matcher restricts the response to the canary's own
    probe label so a busy Alertmanager does not stream back its entire active
    alert set on every retry. ``silenced`` is requested explicitly because the
    probe is silenced before dispatch, and the read-back must still see it.
    """
    query = urlencode({"filter": f'{_CANARY_LABEL_KEY}="{probe_id}"', "active": "true", "silenced": "true"})
    query_url = f"{base_url}{_ALERTS_PATH}?{query}"
    with urlopen(query_url, timeout=_ALERTS_TIMEOUT_S) as resp:  # nosec B310
        raw = resp.read()
    try:
        alerts = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Active alerts response is not valid JSON: {exc}") from exc
    if not isinstance(alerts, list):
        raise RuntimeError(f"Active alerts response must be a JSON array, got {type(alerts).__name__!r}")
    for alert in alerts:
        labels = alert.get("labels", {}) if isinstance(alert, dict) else {}
        if labels.get(_CANARY_LABEL_KEY) == probe_id:
            return True
    return False


def _confirm_canary_cleared(base_url: str, probe_id: str) -> bool:
    """Return True once the resolved canary no longer appears in active alerts.

    A resolve POST only sets ``endsAt``; clock skew or HA propagation can keep
    Alertmanager listing the canary as active for a moment, so poll before the
    caller removes the silence.
    """
    for attempt in range(_RESOLVE_CONFIRM_ATTEMPTS):
        if attempt:
            time.sleep(_RESOLVE_CONFIRM_BACKOFF_S)
        if not _query_canary(base_url, probe_id):
            return True
    return False


def _resolve_canary(base_url: str, probe_id: str) -> None:
    """Resolve the canary alert by re-dispatching it with an ``endsAt`` in the past.

    Alertmanager marks an alert resolved once ``endsAt`` has elapsed. ``endsAt``
    is backdated by a clock-skew allowance so a leading host clock cannot leave
    it in Alertmanager's future. Any error propagates so the caller can report
    the cleanup as failed.
    """
    now = datetime.now(timezone.utc)
    ends_at = now - timedelta(seconds=_CLOCK_SKEW_S)
    starts_at = now - timedelta(minutes=_CANARY_ACTIVE_MIN)  # keep startsAt before endsAt so the resolve is accepted
    _post_alerts(base_url, _canary_payload(probe_id, starts_at, ends_at))


def _create_silence(base_url: str, probe_id: str) -> str:
    """Create a silence matching the canary probe label; return its silence ID.

    Silencing before dispatch prevents Alertmanager from routing the canary to
    real receivers, whose notifications cleanup cannot retract.
    """
    now = datetime.now(timezone.utc)
    body = {
        "matchers": [{"name": _CANARY_LABEL_KEY, "value": probe_id, "isRegex": False, "isEqual": True}],
        # Backdate startsAt so the silence is active even if this host's clock leads Alertmanager's.
        "startsAt": (now - timedelta(seconds=_CLOCK_SKEW_S)).isoformat(),
        "endsAt": (now + timedelta(minutes=_SILENCE_DURATION_MIN)).isoformat(),
        "createdBy": _VALIDATOR_ID,
        "comment": "Silence validator canary probe so it cannot page real receivers.",
    }
    req = Request(f"{base_url}{_SILENCES_PATH}", data=json.dumps(body).encode("utf-8"), method="POST")  # nosec B310
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=_ALERTS_TIMEOUT_S) as resp:  # nosec B310
            raw = resp.read()
            if resp.status not in (200, 201):
                raise RuntimeError(f"Unexpected silence response: HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, "Silence rejected") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Silence response is not valid JSON: {exc}") from exc
    silence_id = ""
    if isinstance(data, dict):
        silence_id = data.get("silenceID") or data.get("silenceId") or ""
    if not silence_id:
        raise RuntimeError("Silence response did not include a silence ID")
    return str(silence_id)


def _confirm_silence_active(base_url: str, silence_id: str) -> None:
    """Poll the silence until Alertmanager reports it ``active``, else raise.

    A returned silence ID only means the silence was accepted; it may still be
    ``pending`` (e.g. a leading host clock), and a pending silence does not mute
    alerts. Callers must not dispatch the canary until this confirms ``active``.
    """
    last_state = "unknown"
    for attempt in range(_SILENCE_SETTLE_ATTEMPTS):
        if attempt:
            time.sleep(_SILENCE_SETTLE_BACKOFF_S)
        req = Request(_silence_url(base_url, silence_id))  # nosec B310
        try:
            with urlopen(req, timeout=_ALERTS_TIMEOUT_S) as resp:  # nosec B310
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, "Silence status query rejected") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Silence status response is not valid JSON: {exc}") from exc
        status = data.get("status", {}) if isinstance(data, dict) else {}
        last_state = status.get("state", "unknown")
        if last_state == "active":
            return
        if last_state == "expired":
            raise RuntimeError(f"Silence {silence_id} expired before it could mute the canary")
    raise RuntimeError(f"Silence {silence_id} did not become active (last state {last_state!r})")


def _delete_silence(base_url: str, silence_id: str) -> None:
    """Delete the canary silence created before dispatch."""
    req = Request(_silence_url(base_url, silence_id), method="DELETE")  # nosec B310
    try:
        with urlopen(req, timeout=_ALERTS_TIMEOUT_S) as resp:  # nosec B310
            resp.read()
            if resp.status not in (200, 204):
                raise RuntimeError(f"Unexpected silence deletion response: HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, "Silence deletion rejected") from exc
