# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import socket
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml  # pyyaml; required for v1 (deprecated YAML wire format) support

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_TCP_TIMEOUT = 5
_HTTP_TIMEOUT = 5


class IngressValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        databag = self.databag

        schema_check, url = _parse_ingress_url(databag)
        checks: list[ValidationCheck] = [schema_check]
        if not schema_check.passed:
            return self._fail_result(level, checks)

        checks.append(_url_format_check(url))
        if not checks[-1].passed:
            return self._fail_result(level, checks)

        if level == "deep":
            host, port = _extract_host_port(url)
            checks.append(_connectivity_check(host, port, url))
            if checks[-1].passed:
                checks.append(_http_probe_check(url))

        return self._make_result(level=level, checks=checks)


# ---------------------------------------------------------------------------
# Pure helpers — ingress URL parsing
# ---------------------------------------------------------------------------


def _parse_ingress_url(databag: dict[str, str]) -> tuple[ValidationCheck, str]:
    """Parse the ingress URL from the provider app databag.

    Supports both v2 (JSON-encoded) and v1 (YAML-encoded) wire formats:
      - v2: ``databag["ingress"] = '{"url": "http://..."}'``
      - v1: ``databag["ingress"] = 'url: http://...\\n'``

    Returns a (check, url) tuple. On failure, url is an empty string.
    """
    raw = databag.get("ingress", "")
    if not raw:
        return (
            ValidationCheck(
                name="schema",
                passed=False,
                message="Missing 'ingress' key in provider app databag.",
            ),
            "",
        )

    ingress_data = _decode_ingress_field(raw)
    if ingress_data is None:
        return (
            ValidationCheck(
                name="schema",
                passed=False,
                message=f"Could not decode 'ingress' field as JSON or YAML: {raw!r}",
            ),
            "",
        )

    if "url" not in ingress_data:
        return (
            ValidationCheck(
                name="schema",
                passed=False,
                message="'ingress' data decoded successfully but 'url' key is missing.",
            ),
            "",
        )

    url = ingress_data["url"]
    if not isinstance(url, str):
        return (
            ValidationCheck(
                name="schema",
                passed=False,
                message=f"'url' value must be a string, got {type(url).__name__}: {url!r}",
            ),
            "",
        )

    if not url:
        return (
            ValidationCheck(
                name="schema",
                passed=False,
                message="'ingress' data decoded successfully but 'url' value is empty.",
            ),
            "",
        )

    return (
        ValidationCheck(
            name="schema",
            passed=True,
            message=f"Ingress URL found: {url!r}",
        ),
        url,
    )


def _decode_ingress_field(raw: str) -> dict[str, Any] | None:
    """Try JSON first (v2), then YAML (v1 deprecated), return None on failure."""
    # v2: JSON-encoded dict
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            return decoded
    except (json.JSONDecodeError, ValueError):
        pass

    # v1 (deprecated): YAML-encoded dict
    try:
        decoded = yaml.safe_load(raw)
        if isinstance(decoded, dict):
            return decoded
    except (yaml.YAMLError, TypeError, ValueError):
        pass

    return None


# ---------------------------------------------------------------------------
# Pure helpers — URL format check
# ---------------------------------------------------------------------------


def _url_format_check(url: str) -> ValidationCheck:
    """Validate that the ingress URL is a well-formed HTTP/HTTPS URL."""
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return ValidationCheck(
            name="url_format",
            passed=False,
            message=f"Failed to parse URL {url!r}: {exc}",
        )

    if parsed.scheme not in ("http", "https"):
        return ValidationCheck(
            name="url_format",
            passed=False,
            message=f"URL scheme {parsed.scheme!r} is not 'http' or 'https'.",
        )
    if not parsed.netloc or not parsed.hostname:
        return ValidationCheck(
            name="url_format",
            passed=False,
            message=f"URL {url!r} has no valid hostname.",
        )

    try:
        _ = parsed.port  # raises ValueError for out-of-range or non-integer ports
    except ValueError as exc:
        return ValidationCheck(
            name="url_format",
            passed=False,
            message=f"URL {url!r} has an invalid port: {exc}",
        )

    return ValidationCheck(
        name="url_format",
        passed=True,
        message=f"URL {url!r} is well-formed.",
    )


# ---------------------------------------------------------------------------
# Pure helpers — deep level checks
# ---------------------------------------------------------------------------


def _extract_host_port(url: str) -> tuple[str, int]:
    """Extract (host, port) from an ingress URL with sensible defaults."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        return host, parsed.port
    return host, 443 if parsed.scheme == "https" else 80


def _connectivity_check(host: str, port: int, url: str) -> ValidationCheck:
    """TCP-ping the ingress endpoint."""
    try:
        _tcp_ping(host, port)
        return ValidationCheck(
            name="connect",
            passed=True,
            message=f"TCP reached {host}:{port}.",
        )
    except Exception as exc:
        return ValidationCheck(
            name="connect",
            passed=False,
            message=f"TCP connection to {host}:{port} (from {url!r}) failed: {exc}",
        )


def _http_probe_check(url: str) -> ValidationCheck:
    """Issue an HTTP GET to the ingress URL and verify a valid HTTP response."""
    try:
        req = Request(url)  # nosec B310 - url is http/https only
        with urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
            status = resp.status
        return ValidationCheck(
            name="http_probe",
            passed=True,
            message=f"HTTP GET {url} -> {status}.",
        )
    except HTTPError as exc:
        # Any HTTP status code proves the ingress is active and routing traffic.
        # HTTPError is also a file-like response object; close it to release the socket.
        code = exc.code
        exc.close()
        return ValidationCheck(
            name="http_probe",
            passed=True,
            message=f"HTTP GET {url} -> {code} (HTTP service reachable).",
        )
    except Exception as exc:
        return ValidationCheck(
            name="http_probe",
            passed=False,
            message=f"HTTP GET {url} failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Low-level network helpers
# ---------------------------------------------------------------------------


def _tcp_ping(host: str, port: int, timeout: float = float(_TCP_TIMEOUT)) -> None:
    """Open a TCP connection to host:port and immediately close it."""
    with socket.create_connection((host, port), timeout=timeout):
        pass
