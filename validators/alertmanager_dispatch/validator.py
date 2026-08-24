# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import socket
import time
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

logger = logging.getLogger(__name__)

_HEALTHY_PATH = "/-/healthy"
_ALERTS_PATH = "/api/v2/alerts"
_CANARY_ALERTNAME = "EndpointValidatorCanary"
_CANARY_LABEL_KEY = "validator_probe"
_INGEST_WAIT_S = 2
_QUERY_ATTEMPTS = 3
_HEALTH_ATTEMPTS = 3
_HEALTH_BACKOFF_S = 3
_HEALTH_TIMEOUT_S = 5
_ALERTS_TIMEOUT_S = 10


class AlertmanagerDispatchValidator(BaseValidator):
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

        checks.extend(_http_healthy_checks(endpoint_infos))
        if level == "deep" and all(c.passed for c in checks):
            checks.extend(_canary_checks(endpoint_infos))

        return self._make_result(level=level, checks=checks)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _base_url(url: str) -> str:
    """Return *url* without a trailing slash so API paths can be appended."""
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# Pure helpers — endpoint collection from provider unit databags
# ---------------------------------------------------------------------------


def _collect_endpoint_infos(
    relation: Any,
) -> tuple[list[dict[str, str]], list[str]]:
    """Return deduplicated endpoint info dicts and any collection errors.

    The ``alertmanager_dispatch`` provider (Alertmanager) writes its workload
    URL to each of its **unit** databags. Two on-wire shapes exist:

    * v1: ``relation.data[unit]["url"] = "<scheme>://<host>:<port>"``
    * v0: ``relation.data[unit]["public_address"] = "<host>:<port>"`` with an
      optional ``relation.data[unit]["scheme"]`` (defaults to ``http``).

    ``receiver`` is not part of the interface's relation data (Alertmanager
    receivers are server-side routing config), so it is collected only when a
    charm happens to advertise it and is validated opportunistically.

    Malformed entries are returned as errors so ``_schema_check`` can surface
    every bad databag in one check.
    """
    infos: list[dict[str, str]] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    for unit in sorted(relation.units, key=lambda u: getattr(u, "name", repr(u))):
        unit_name = getattr(unit, "name", repr(unit))
        data = relation.data[unit]
        if not data:
            continue  # a provider unit with an empty databag has nothing to advertise yet

        url = data.get("url", "")
        if not url:
            public_address = data.get("public_address", "")
            if public_address:
                scheme = data.get("scheme", "http") or "http"
                url = f"{scheme}://{public_address}"

        if not url:
            errors.append(f"Unit {unit_name!r}: no 'url' or 'public_address' key in databag")
            continue
        if url in seen_urls:
            continue  # dedup — same URL from a scaled-out provider is not an error
        seen_urls.add(url)

        info: dict[str, str] = {"url": url}
        if "receiver" in data:
            info["receiver"] = data["receiver"]
        infos.append(info)
    return infos, errors


# ---------------------------------------------------------------------------
# Pure helpers — schema
# ---------------------------------------------------------------------------


def _schema_check(
    endpoint_infos: list[dict[str, str]],
    collection_errors: list[str],
) -> ValidationCheck:
    """Validate structure and value constraints of every advertised endpoint.

    Per endpoint:

    * ``url`` must be non-empty, use an http/https scheme, and have a hostname.
    * ``receiver`` (optional) — when present, must be a non-empty string.
    """
    if not endpoint_infos and not collection_errors:
        return ValidationCheck(
            name="schema",
            passed=False,
            message="No alertmanager_dispatch data found in provider unit databags.",
        )

    errors: list[str] = list(collection_errors)
    for info in endpoint_infos:
        url = info.get("url", "")
        if not url:
            errors.append("'url' is empty")
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            errors.append(f"{url!r}: unsupported scheme {parsed.scheme!r}")
            continue
        if not parsed.hostname:
            errors.append(f"{url!r}: missing hostname")
            continue

        if "receiver" in info and not info["receiver"].strip():
            errors.append(f"{url!r}: 'receiver' must be a non-empty string when present")

    if errors:
        return ValidationCheck(name="schema", passed=False, message="; ".join(errors))
    return ValidationCheck(name="schema", passed=True, message=f"Validated {len(endpoint_infos)} endpoint(s).")


# ---------------------------------------------------------------------------
# Pure helpers — L1 connectivity & health
# ---------------------------------------------------------------------------


def _tcp_ping(host: str, port: int, timeout: float = 5.0) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


def _connectivity_check(endpoint_infos: list[dict[str, str]]) -> ValidationCheck:
    """TCP-ping every Alertmanager endpoint; return a single pass/fail check."""
    errors: list[str] = []
    for info in endpoint_infos:
        url = info["url"]
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


def _http_healthy_checks(endpoint_infos: list[dict[str, str]]) -> list[ValidationCheck]:
    """HTTP GET ``/-/healthy`` for each Alertmanager URL; confirm 200 OK.

    Alertmanager may take a few seconds to become healthy after startup, so the
    check retries with back-off before reporting failure.
    """
    checks: list[ValidationCheck] = []
    for info in endpoint_infos:
        url = info["url"]
        parsed = urlparse(url)
        healthy_url = f"{_base_url(url)}{_HEALTHY_PATH}"
        check_name = f"http_healthy[{parsed.netloc}]"
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
                        last_msg = f"Alertmanager healthy at {healthy_url}."
                        break
                    last_msg = f"Unexpected status {resp.status} from {healthy_url}."
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                last_msg = f"Unexpected response {exc.code} from {healthy_url}: {body[:200]}"
            except Exception as exc:
                last_msg = str(exc)
        checks.append(ValidationCheck(name=check_name, passed=passed, message=last_msg))
    return checks


# ---------------------------------------------------------------------------
# Pure helpers — L2 canary dispatch round-trip
# ---------------------------------------------------------------------------


def _canary_checks(endpoint_infos: list[dict[str, str]]) -> list[ValidationCheck]:
    """Dispatch a canary alert to each endpoint, read it back, then resolve it.

    Uses a unique per-run probe label so concurrent validator runs do not
    cross-pollinate. Returns one check per endpoint covering push + query;
    the resolve step is best-effort cleanup and never fails the check.
    """
    checks: list[ValidationCheck] = []
    for info in endpoint_infos:
        url = info["url"]
        parsed = urlparse(url)
        base = _base_url(url)
        check_name = f"canary[{parsed.netloc}]"
        probe_id = uuid.uuid4().hex[:12]

        try:
            _push_canary(base, probe_id)
        except Exception as exc:
            checks.append(ValidationCheck(name=check_name, passed=False, message=f"Dispatch failed: {exc}"))
            continue

        found = False
        query_error = ""
        for _ in range(_QUERY_ATTEMPTS):
            time.sleep(_INGEST_WAIT_S)
            try:
                found = _query_canary(base, probe_id)
            except Exception as exc:
                query_error = str(exc)
                continue
            query_error = ""  # a successful query supersedes any earlier transient error
            if found:
                break

        _resolve_canary(base, probe_id)  # best-effort cleanup

        if found:
            checks.append(
                ValidationCheck(
                    name=check_name,
                    passed=True,
                    message=f"Canary alert dispatched and read back from {parsed.netloc}.",
                )
            )
        elif query_error:
            checks.append(ValidationCheck(name=check_name, passed=False, message=f"Query failed: {query_error}"))
        else:
            checks.append(
                ValidationCheck(
                    name=check_name,
                    passed=False,
                    message=f"Canary alert dispatched to {parsed.netloc} but not found in active alerts.",
                )
            )
    return checks


def _canary_payload(probe_id: str, ends_at: datetime) -> bytes:
    now = datetime.now(timezone.utc)
    alert = [
        {
            "labels": {
                "alertname": _CANARY_ALERTNAME,
                _CANARY_LABEL_KEY: probe_id,
                "severity": "info",
            },
            "annotations": {"summary": "canary alert emitted by the alertmanager_dispatch validator"},
            "startsAt": now.isoformat(),
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
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"Dispatch rejected: HTTP {exc.code}: {body[:200]}") from exc


def _push_canary(base_url: str, probe_id: str) -> None:
    """POST a single canary alert active for five minutes to *base_url*."""
    ends_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    _post_alerts(base_url, _canary_payload(probe_id, ends_at))


def _query_canary(base_url: str, probe_id: str) -> bool:
    """Query the active alerts endpoint; return True if the canary is present.

    A server-side ``filter`` matcher restricts the response to the canary's own
    probe label so a busy Alertmanager does not stream back its entire active
    alert set on every retry.
    """
    matcher = urlencode({"filter": f'{_CANARY_LABEL_KEY}="{probe_id}"'})
    query_url = f"{base_url}{_ALERTS_PATH}?{matcher}"
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


def _resolve_canary(base_url: str, probe_id: str) -> None:
    """Resolve the canary alert by re-dispatching it with an ``endsAt`` in the past.

    Best-effort cleanup: Alertmanager marks an alert resolved once ``endsAt``
    has elapsed, so any error here is intentionally swallowed.
    """
    ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    try:
        _post_alerts(base_url, _canary_payload(probe_id, ends_at))
    except Exception as exc:
        logger.debug("Best-effort resolve of canary alert %s failed: %s", probe_id, exc)
