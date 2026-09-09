# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import ipaddress
import json
import re
import socket
import ssl
from http.client import HTTPMessage
from typing import IO, Any
from urllib.error import HTTPError
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, OpenerDirector, ProxyHandler, Request, build_opener

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_TCP_TIMEOUT = 5
_HTTP_TIMEOUT = 5

_HOSTNAME_LABEL = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
_HOSTNAME_RE = re.compile(rf"^{_HOSTNAME_LABEL}(\.{_HOSTNAME_LABEL})*$")


def _redact(value: str) -> str:
    """Return a display-safe copy of an untrusted external_host/URL value.

    A malformed 'external_host' (e.g. 'user:secret@host', 'host/path?token=secret',
    or 'host:some-secret' disguised as a port) would otherwise place a secret
    directly in the validator's JSON result, since result messages echo the value
    verbatim before it has been validated — including the schema-check message,
    which runs before URL-format validation has had a chance to reject a malformed
    port. Strip any query-string/fragment suffix, user-info component, and
    non-numeric/out-of-range port before interpolating an untrusted value into a
    message.
    """
    for sep in ("?", "#"):
        idx = value.find(sep)
        if idx != -1:
            value = value[:idx]

    scheme_sep = value.find("://")
    prefix, rest = (value[: scheme_sep + 3], value[scheme_sep + 3 :]) if scheme_sep != -1 else ("", value)

    path_idx = rest.find("/")
    authority = rest if path_idx == -1 else rest[:path_idx]
    remainder = "" if path_idx == -1 else rest[path_idx:]

    at_idx = authority.rfind("@")
    if at_idx != -1:
        authority = authority[at_idx + 1 :]

    host_part, sep, port_part = authority.rpartition(":")
    if sep and not (port_part.isdigit() and int(port_part) <= 65535):
        authority = f"{host_part}:<redacted>"

    return prefix + authority + remainder


class _NoRedirectHandler(HTTPRedirectHandler):
    """Raise HTTPError on 3xx instead of following the redirect target.

    Any HTTP response (including a redirect) proves the gateway is live, so the
    probe must not silently follow to whatever host the redirect names.
    """

    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> Request | None:
        raise HTTPError(req.full_url, code, msg, headers, fp)


def _insecure_https_context() -> ssl.SSLContext:
    """Build a TLS context that skips certificate verification for https:// probes.

    This relation exposes only ``external_host`` and ``tls_enabled``: unlike interfaces
    that carry a CA bundle in relation data, there is no trust path here to whatever
    private/self-signed CA Istio's own TLS integration tests configure the gateway with
    (its integration tests supply a custom CA). Without it, a healthy gateway serving
    such a certificate would raise ``CERTIFICATE_VERIFY_FAILED`` before any HTTP
    response is observed. L2 only proves the gateway is reachable and routing traffic,
    not that its certificate is trusted, so skip verification here the same way a basic
    reachability check (e.g. ``curl -k``) would.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _build_opener() -> OpenerDirector:
    """Build the opener used for HTTP probes.

    Factored out (instead of inlined at module scope) so tests can invoke the exact
    production construction path under a patched environment, rather than
    independently rebuilding an opener that could silently drift out of sync with it.

    ProxyHandler({}) disables http_proxy/https_proxy so the probe reaches the ingress
    gateway directly, instead of a CI/dev proxy whose response could otherwise be
    misread as success. HTTPSHandler(context=...) skips TLS certificate verification for
    https:// probes (see _insecure_https_context) and is a no-op for http:// requests.
    """
    return build_opener(ProxyHandler({}), _NoRedirectHandler, HTTPSHandler(context=_insecure_https_context()))


_opener = _build_opener()


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

    ``external_host`` never encodes a port: istio_ingress_route lets a requirer
    declare arbitrary Gateway listener ports for its routes (not just 80/443) via
    a ``config`` key the requirer publishes into its *own* local application
    databag on this relation (JSON-encoded, with a ``listeners`` list of
    ``{"port": ..., "protocol": "HTTP" | "GRPC"}``). L2 reads that local config to
    determine which port(s) to probe, instead of assuming a default, and probes
    every declared HTTP-protocol listener (a requirer may declare more than one).
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
            local_databag = dict(self.relation.data[self.charm.app])
            ports, port_check = _resolve_probe_ports(url, local_databag)
            if not ports:
                if port_check is not None:
                    checks.append(port_check)
                if port_check is not None and not port_check.passed:
                    return self._fail_result(level, checks)
                # Neither the TCP nor HTTP capability check actually ran here (no
                # config has been published yet, or it declares no HTTP-protocol
                # listener), so this is not a successful deep validation based on
                # a real probe. Report SKIPPED rather than PASS so downstream
                # automation doesn't record it as one.
                return self._make_result(status="SKIPPED", level=level, checks=checks)

            host = _extract_host(url)
            for port in ports:
                probe_url = _with_port(url, port)
                connect_check = _connectivity_check(host, port, probe_url)
                checks.append(connect_check)
                if connect_check.passed:
                    checks.append(_http_probe_check(probe_url))

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
            message=f"Ingress endpoint found: external_host={_redact(external_host)!r}, tls_enabled={tls_enabled}.",
        ),
        url,
    )


# ---------------------------------------------------------------------------
# Pure helpers — URL format check
# ---------------------------------------------------------------------------


def _url_format_check(url: str) -> ValidationCheck:
    """Validate that the derived ingress URL is a well-formed HTTP/HTTPS URL."""
    # Redact before interpolating into any message: at this point url has not been
    # validated, so it may still carry a query string or user-info smuggled in via a
    # malformed 'external_host' (see _redact for details).
    display = _redact(url)

    # urlparse silently strips \t, \r, and \n (and tolerates other control characters),
    # so a value like "good.example\n" would otherwise normalize to a valid-looking host.
    if not url.isprintable():
        return ValidationCheck(
            name="url_format",
            passed=False,
            message=f"URL {display!r} contains control characters that are not valid in a bare host.",
        )

    # Unescaped whitespace (e.g. a literal space in a path segment) is not valid in a
    # URL and urllib rejects it at request time, even though urlparse accepts it and
    # str.isprintable() treats plain spaces as printable. Percent-encoded whitespace
    # (e.g. "%20") contains no literal whitespace character, so it is unaffected.
    if any(char.isspace() for char in url):
        return ValidationCheck(
            name="url_format",
            passed=False,
            message=f"URL {display!r} contains unescaped whitespace; percent-encode it (e.g. '%20') instead.",
        )

    # urllib requires an ASCII URI; a raw (non-percent-encoded) non-ASCII character in
    # the path, e.g. "example.com/café", passes urlparse but raises UnicodeEncodeError
    # at request time during the deep-level HTTP probe. Percent-encoded paths are ASCII
    # and remain valid.
    if not url.isascii():
        return ValidationCheck(
            name="url_format",
            passed=False,
            message=f"URL {display!r} contains raw non-ASCII characters; percent-encode them instead.",
        )

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return ValidationCheck(
            name="url_format",
            passed=False,
            message=f"Failed to parse URL {display!r}: {exc}",
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
            message=f"URL {display!r} has no valid hostname.",
        )

    # parsed.query/fragment are '' both when the component is absent and when it is
    # syntactically present but empty (e.g. "host?" or "host#"), so a truthiness check
    # misses those cases; likewise parsed.username is '' (not None) only when user-info
    # is present but empty (e.g. "@host"), so checking "is not None" catches it while a
    # bare host still yields None. Inspect the raw delimiters/attributes explicitly.
    has_query = "?" in url
    has_fragment = "#" in url
    has_userinfo = parsed.username is not None or parsed.password is not None
    has_params = ";" in parsed.path

    if has_params or has_query or has_fragment or has_userinfo:
        return ValidationCheck(
            name="url_format",
            passed=False,
            message=f"URL {display!r} contains a query/fragment/user-info; 'external_host' must not include them.",
        )

    if not _is_valid_host(parsed.hostname):
        return ValidationCheck(
            name="url_format",
            passed=False,
            message=f"URL {display!r} has an invalid hostname {parsed.hostname!r}.",
        )

    try:
        _ = parsed.port  # raises ValueError for out-of-range or non-integer ports
    except ValueError:
        # str(exc) would restate the raw invalid port text (e.g. from
        # "host:some-secret"), which may itself be untrusted/attacker-controlled,
        # so it must not be echoed here even though 'display' already redacts it.
        return ValidationCheck(
            name="url_format",
            passed=False,
            message=f"URL {display!r} has an invalid port.",
        )

    return ValidationCheck(
        name="url_format",
        passed=True,
        message=f"URL {display!r} is well-formed.",
    )


def _is_valid_host(host: str) -> bool:
    """Return True if host is a valid IPv4/IPv6 address or DNS hostname (RFC 1123)."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    # A single trailing dot denotes an absolute (fully-qualified) DNS name, which is
    # valid and resolvable; strip at most one before checking the label grammar/length
    # so a name with two or more trailing dots is still correctly rejected.
    dns_name = host[:-1] if host.endswith(".") else host
    return len(dns_name) <= 253 and bool(_HOSTNAME_RE.fullmatch(dns_name))


# ---------------------------------------------------------------------------
# Deep level checks
# ---------------------------------------------------------------------------


def _extract_host(url: str) -> str:
    """Extract the hostname from an ingress URL."""
    return urlparse(url).hostname or ""


def _resolve_probe_ports(url: str, local_databag: dict[str, str]) -> tuple[list[int], ValidationCheck | None]:
    """Determine which port(s) the deep-level checks should probe.

    ``external_host`` (see class docstring) never itself encodes a port unless the
    provider chose to publish one explicitly; when it does, that value is
    unambiguous and used as-is. Otherwise, the actual listener port(s) are only
    known from the requirer's own locally-published ``config`` (see class
    docstring), so they must be read from there rather than assumed to be
    80/443. A requirer may declare more than one HTTP-protocol listener, and any
    one of them being unreachable is a real routing problem, so every declared
    HTTP port is returned (deduplicated, order preserved) rather than only the
    first.

    A URL carrying a path (see ``test_passes_simple_when_external_host_carries_upstream_route_path``)
    means ``external_host`` is a chained deployment: the provider is itself behind
    another ingress hop, and the path routes to it there. The requirer's locally
    declared listener ports describe the *inner* Istio gateway behind that hop, not
    this outer one, so applying them here would corrupt an otherwise-valid URL (e.g.
    turning ``https://upstream.example/model-app`` into
    ``https://upstream.example:8080/model-app``, an address the outer hop never
    listens on). Probe the scheme's conventional external port instead in that case.

    Returns (ports, check). ``check`` is only set (and ``ports`` is empty) when no
    usable port could be determined, so callers can skip the connectivity/probe
    checks instead of guessing at a port that may not have a listener behind it.
    """
    parsed = urlparse(url)
    if parsed.port is not None:
        return [parsed.port], None

    if parsed.path:
        return [443 if parsed.scheme == "https" else 80], None

    raw_config = local_databag.get("config")
    if not raw_config:
        return [], ValidationCheck(
            name="connect",
            passed=True,
            message="No local 'config' published on this relation; deep connectivity check skipped.",
        )

    try:
        listeners = _parse_listeners(raw_config)
        http_ports = list(dict.fromkeys(int(listener["port"]) for listener in listeners if _is_http_listener(listener)))
    except (TypeError, ValueError, KeyError) as exc:
        return [], ValidationCheck(
            name="connect",
            passed=False,
            message=f"Failed to parse local 'config': {exc}",
        )

    if not http_ports:
        return [], ValidationCheck(
            name="connect",
            passed=True,
            message="No HTTP-protocol listener declared in local 'config'; deep connectivity check skipped.",
        )

    return http_ports, None


def _parse_listeners(raw_config: str) -> list[dict[str, Any]]:
    """Decode and validate the 'listeners' list from a requirer's local 'config'.

    Raises TypeError/ValueError/KeyError if 'config' is not valid JSON, 'listeners' is
    missing or not a list, or any entry lacks a supported 'protocol' or a 'port' in the
    interface's valid 1-65535 range. A malformed entry (e.g. {"port": 8080} with no
    protocol) must not be silently filtered out alongside genuinely absent GRPC-only
    listeners: that would hide a broken local contract behind a false "nothing to
    probe, so skip" result instead of surfacing it as a failure.
    """
    listeners = json.loads(raw_config)["listeners"]
    if not isinstance(listeners, list):
        raise TypeError(f"'listeners' must be a list, got {type(listeners).__name__}.")
    for listener in listeners:
        if not isinstance(listener, dict):
            raise TypeError(f"listener entry must be an object, got {type(listener).__name__}.")
        # ProtocolType's wire values are exactly "HTTP"/"GRPC" (the provider's Pydantic
        # model rejects any other casing), so compare the raw value directly instead of
        # normalizing case: a lowercase "http" is itself a malformed local contract that
        # the provider could never have parsed, not a valid-but-differently-cased value.
        if listener.get("protocol") not in ("HTTP", "GRPC"):
            raise ValueError(f"listener has an unsupported 'protocol': {listener.get('protocol')!r}.")
        port = listener.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
            raise ValueError(f"listener has an invalid 'port': {port!r}.")
    return listeners


def _is_http_listener(listener: object) -> bool:
    """Return True if a decoded listener entry declares the HTTP application protocol.

    GRPC listeners are excluded: they speak HTTP/2 framed gRPC, not plain HTTP, so an
    HTTP GET probe against one would not exercise a real request/response cycle. The
    wire value is compared verbatim (not case-normalized): see _parse_listeners.
    """
    return isinstance(listener, dict) and listener.get("protocol") == "HTTP"


def _with_port(url: str, port: int) -> str:
    """Return url with an explicit ':port' set on its authority component (idempotent)."""
    parsed = urlparse(url)
    if parsed.port == port:
        return url
    host = parsed.hostname or ""
    if ":" in host:  # bracket a bare IPv6 literal so "host:port" stays unambiguous
        host = f"[{host}]"
    return urlunparse(parsed._replace(netloc=f"{host}:{port}"))


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
        with _opener.open(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
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
