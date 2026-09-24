# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import ipaddress
import re
import socket
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import yaml  # pyyaml; the provider publishes its unit->URL mapping as a YAML-encoded string

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

# Required fields the requirer publishes in its own unit databag.
_REQUIRED_FIELDS = ("name", "host", "port", "model")

# The provider's application databag key holding the unit->URL mapping. The published
# interface spec names it "urls", but the reference traefik-k8s library (v1) publishes
# the same mapping under "ingress"; accept either so the validator works against both.
_PROVIDER_KEYS = ("ingress", "urls")

_TCP_TIMEOUT = 5
_HTTP_TIMEOUT = 10

_MIN_PORT = 1
_MAX_PORT = 65535

_HOSTNAME_LABEL = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
_HOSTNAME_RE = re.compile(rf"^{_HOSTNAME_LABEL}(\.{_HOSTNAME_LABEL})*$")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_HTTP_OPENER = build_opener(ProxyHandler({}), _NoRedirectHandler())


class IngressPerUnitValidator(BaseValidator):
    """Validator for the ``ingress_per_unit`` Juju interface.

    The requirer publishes ``name``, ``host``, ``port`` and ``model`` in the unit
    databag of *each* of its units, describing the endpoint it wants exposed. The
    provider publishes a mapping from unit name to ingress URL in its application
    databag, encoded as YAML.

    Validation levels:
      * simple (L1): this unit's own databag advertises a well-formed endpoint.
      * deep   (L2): the provider advertises a well-formed ingress URL for this unit
        and that URL is reachable.
    """

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        checks: list[ValidationCheck] = []
        local = self._local_unit_databag()

        schema_check = self.validate_schema(list(_REQUIRED_FIELDS), data=local)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._fail_result(level, checks)

        host_check = _host_format_check(local["host"])
        checks.append(host_check)
        if not host_check.passed:
            return self._fail_result(level, checks)

        port_check = _port_range_check(local["port"])
        checks.append(port_check)
        if not port_check.passed:
            return self._fail_result(level, checks)

        if level == "simple":
            return self._make_result(level=level, checks=checks)

        urls_check, urls = _decode_provider_urls(self.databag)
        checks.append(urls_check)
        if not urls_check.passed:
            return self._fail_result(level, checks)

        unit_name = self.charm.unit.name
        url_check, url = _unit_url_check(urls, unit_name)
        checks.append(url_check)
        if not url_check.passed:
            return self._fail_result(level, checks)

        format_check = _url_format_check(url)
        checks.append(format_check)
        if not format_check.passed:
            return self._fail_result(level, checks)

        host, port = _extract_host_port(url)
        checks.append(_connectivity_check(host, port, url))
        if checks[-1].passed:
            checks.append(_http_probe_check(url))

        return self._make_result(level=level, checks=checks)

    def _local_unit_databag(self) -> dict[str, str]:
        """Read this unit's own contribution to the relation.

        Unlike ``self.databag`` (the remote application's data), the requirer's
        ``name``/``host``/``port``/``model`` fields live in this unit's own databag,
        so they must be read directly from ``self.relation.data``. Mirrors
        ``BaseValidator.databag``'s defensive lookup so a unit that hasn't published
        yet yields a normal schema FAIL rather than a ``KeyError``.
        """
        if self.charm.unit not in self.relation.data:
            return {}
        return dict(self.relation.data[self.charm.unit])


# ---------------------------------------------------------------------------
# Pure helpers — requirer unit databag
# ---------------------------------------------------------------------------


def _host_format_check(host: str) -> ValidationCheck:
    """Confirm *host* is a plausible hostname (no scheme, no whitespace, no port)."""
    if not host or host != host.strip():
        return ValidationCheck(
            name="host_format",
            passed=False,
            message=f"host {host!r} is empty or has surrounding whitespace.",
        )
    if "://" in host:
        return ValidationCheck(
            name="host_format",
            passed=False,
            message=f"host {host!r} must be a bare hostname, not a URL.",
        )
    if any(c.isspace() for c in host):
        return ValidationCheck(
            name="host_format",
            passed=False,
            message=f"host {host!r} contains whitespace.",
        )
    if any(character in host for character in ":/@"):
        return ValidationCheck(
            name="host_format",
            passed=False,
            message=f"host {host!r} must be a bare hostname or IPv4 address.",
        )
    return ValidationCheck(name="host_format", passed=True, message=f"host {host!r} is well-formed.")


def _port_range_check(port: str) -> ValidationCheck:
    """Confirm *port* is an integer within the valid TCP range."""
    try:
        value = int(port)
    except (TypeError, ValueError):
        return ValidationCheck(
            name="port_range",
            passed=False,
            message=f"port {port!r} is not an integer.",
        )
    if not _MIN_PORT <= value <= _MAX_PORT:
        return ValidationCheck(
            name="port_range",
            passed=False,
            message=f"port {value} is outside the valid range {_MIN_PORT}-{_MAX_PORT}.",
        )
    return ValidationCheck(name="port_range", passed=True, message=f"port {value} is valid.")


# ---------------------------------------------------------------------------
# Pure helpers — provider application databag
# ---------------------------------------------------------------------------


def _decode_provider_urls(databag: dict[str, str]) -> tuple[ValidationCheck, dict[str, str]]:
    """Decode the provider's unit->URL mapping from its application databag.

    Accepts either the spec's ``urls`` key or the reference library's ``ingress``
    key, and both value shapes (``{unit: "url"}`` and ``{unit: {"url": "url"}}``).
    Returns a (check, mapping) pair; mapping is empty on failure.
    """
    raw = ""
    key = ""
    for candidate in _PROVIDER_KEYS:
        if databag.get(candidate):
            raw = databag[candidate]
            key = candidate
            break

    if not raw:
        return (
            ValidationCheck(
                name="provider_urls",
                passed=False,
                message=f"Provider app databag has none of {', '.join(_PROVIDER_KEYS)}.",
            ),
            {},
        )

    try:
        decoded = yaml.safe_load(raw)
    except yaml.YAMLError:
        return (
            ValidationCheck(
                name="provider_urls",
                passed=False,
                message=f"Could not decode provider '{key}' field as YAML.",
            ),
            {},
        )

    if not isinstance(decoded, dict):
        return (
            ValidationCheck(
                name="provider_urls",
                passed=False,
                message=f"Provider '{key}' field must decode to a mapping, got {type(decoded).__name__}.",
            ),
            {},
        )

    urls: dict[str, str] = {}
    for unit_name, value in decoded.items():
        if isinstance(value, dict):
            value = value.get("url")
        if isinstance(value, str) and value:
            urls[str(unit_name)] = value

    if not urls:
        return (
            ValidationCheck(
                name="provider_urls",
                passed=False,
                message=f"Provider '{key}' field decoded but contains no unit->URL entries.",
            ),
            {},
        )

    return (
        ValidationCheck(
            name="provider_urls",
            passed=True,
            message=f"Provider advertises ingress for {len(urls)} unit(s).",
        ),
        urls,
    )


def _unit_url_check(urls: dict[str, str], unit_name: str) -> tuple[ValidationCheck, str]:
    """Confirm *unit_name* has an advertised ingress URL."""
    url = urls.get(unit_name, "")
    if not url:
        return (
            ValidationCheck(
                name="unit_url",
                passed=False,
                message=(
                    f"No ingress URL advertised for unit '{unit_name}'. "
                    f"Advertised units: {', '.join(sorted(urls)) or '(none)'}."
                ),
            ),
            "",
        )
    return (
        ValidationCheck(name="unit_url", passed=True, message=f"Ingress URL for '{unit_name}' is advertised."),
        url,
    )


# ---------------------------------------------------------------------------
# Pure helpers — URL format and connectivity
# ---------------------------------------------------------------------------


def _redact_url(url: str) -> str:
    """Return *url* reduced to its scheme and authority (``scheme://host[:port]``) for display.

    An advertised ingress URL is provider-controlled and its path, query string, or fragment
    may carry a secret (for example ``/secret-token`` or ``?token=...``); strip all of them
    before echoing the URL into a diagnostic message so secrets don't end up in the
    ``ValidationResult`` or runner logs.
    """
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    authority_start = scheme_sep + len("://")
    authority_end = len(url)
    for i in range(authority_start, len(url)):
        if url[i] in "/?#":
            authority_end = i
            break
    return url[:authority_end]


def _url_format_check(url: str) -> ValidationCheck:
    """Validate that the ingress URL is a well-formed HTTP/HTTPS URL."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ValidationCheck(
            name="url_format",
            passed=False,
            message="Ingress URL could not be parsed.",
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
            message="Ingress URL has no valid hostname.",
        )
    if parsed.username is not None or parsed.password is not None:
        return ValidationCheck(name="url_format", passed=False, message="Ingress URL must not contain userinfo.")
    if not _is_valid_host(parsed.hostname):
        return ValidationCheck(
            name="url_format",
            passed=False,
            message="Ingress URL has an invalid hostname.",
        )
    if parsed.netloc.endswith(":"):
        return ValidationCheck(
            name="url_format",
            passed=False,
            message="Ingress URL has an empty port.",
        )

    try:
        _ = parsed.port  # raises ValueError for out-of-range or non-integer ports
    except ValueError:
        return ValidationCheck(
            name="url_format",
            passed=False,
            message="Ingress URL has an invalid port.",
        )

    return ValidationCheck(name="url_format", passed=True, message=f"URL {_redact_url(url)!r} is well-formed.")


def _is_valid_host(host: str) -> bool:
    """Return True if host is a valid IPv4/IPv6 address or DNS hostname."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    dns_name = host[:-1] if host.endswith(".") else host
    return len(dns_name) <= 253 and bool(_HOSTNAME_RE.fullmatch(dns_name))


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
        return ValidationCheck(name="connect", passed=True, message=f"TCP reached {host}:{port}.")
    except Exception as exc:
        return ValidationCheck(
            name="connect",
            passed=False,
            message=f"TCP connection to {host}:{port} failed: {exc}",
        )


def _http_probe_check(url: str) -> ValidationCheck:
    """Issue an HTTP GET to the ingress URL and verify a valid HTTP response."""
    try:
        req = Request(url)
        with _HTTP_OPENER.open(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
            status = resp.status
        return ValidationCheck(name="http_probe", passed=True, message=f"HTTP probe returned status {status}.")
    except HTTPError as exc:
        # Any HTTP status code proves the ingress is active and routing traffic.
        # HTTPError is also a file-like response object; close it to release the socket.
        code = exc.code
        exc.close()
        return ValidationCheck(
            name="http_probe",
            passed=True,
            message=f"HTTP probe returned status {code} (service reachable).",
        )
    except Exception as exc:
        return ValidationCheck(name="http_probe", passed=False, message=f"HTTP probe failed: {exc}")


def _tcp_ping(host: str, port: int, timeout: float = float(_TCP_TIMEOUT)) -> None:
    """Open a TCP connection to host:port and immediately close it."""
    with socket.create_connection((host, port), timeout=timeout):
        pass
