# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import socket
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_TCP_TIMEOUT = 5
_HTTP_TIMEOUT = 5


class IstioIngressRouteValidator(BaseValidator):
    """Validator for the ``istio_ingress_route`` interface.

    Runs on the requirer side (e.g. katib-ui, kfp-ui, feast-ui) and inspects the
    provider (istio-ingress-k8s) application databag, which publishes:
      - ``external_host``: the external hostname/address of the Istio gateway
      - ``tls_enabled``: ``"True"`` or ``"False"`` (stringified bool)

    The requirer derives its external URL as ``{scheme}://{external_host}`` where
    scheme is ``https`` when TLS is enabled, otherwise ``http``. L1 validates the
    published fields and URL shape; L2 probes the gateway to confirm it is
    reachable and routing traffic.
    """

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        databag = self.databag

        schema_check = self.validate_schema(["external_host", "tls_enabled"])
        checks: list[ValidationCheck] = [schema_check]
        if not schema_check.passed:
            return self._fail_result(level, checks)

        schema_check, url = _parse_ingress_endpoint(databag)
        checks[0] = schema_check
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
# Pure helpers — provider databag parsing
# ---------------------------------------------------------------------------


def _parse_ingress_endpoint(databag: dict[str, str]) -> tuple[ValidationCheck, str]:
    """Validate the provider app databag and build the external ingress URL.

    Returns a (check, url) tuple. On failure, url is an empty string.
    """
    external_host = databag.get("external_host", "")
    tls_enabled = databag.get("tls_enabled")

    if tls_enabled not in ("True", "False"):
        return (
            ValidationCheck(
                name="schema",
                passed=False,
                message=f"'tls_enabled' must be 'True' or 'False', got {tls_enabled!r}.",
            ),
            "",
        )

    scheme = "https" if tls_enabled == "True" else "http"
    url = f"{scheme}://{external_host}"
    return (
        ValidationCheck(
            name="schema",
            passed=True,
            message=f"Ingress endpoint found: external_host={external_host!r}, tls_enabled={tls_enabled}.",
        ),
        url,
    )


# ---------------------------------------------------------------------------
# Pure helpers — URL format check
# ---------------------------------------------------------------------------


def _url_format_check(url: str) -> ValidationCheck:
    """Validate that the derived ingress URL is a well-formed HTTP/HTTPS URL."""
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
# Deep level checks
# ---------------------------------------------------------------------------


def _extract_host_port(url: str) -> tuple[str, int]:
    """Extract (host, port) from an ingress URL with sensible defaults."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        return host, parsed.port
    return host, 443 if parsed.scheme == "https" else 80


def _connectivity_check(host: str, port: int, url: str) -> ValidationCheck:
    """TCP-ping the Istio ingress gateway."""
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
    """Issue an HTTP GET to the ingress URL and verify a valid HTTP response.

    Any HTTP status code proves the Istio ingress gateway is active and routing
    traffic, so an HTTP error response is treated as a successful probe.
    """
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
