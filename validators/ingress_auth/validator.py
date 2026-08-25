# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import socket
from http.client import BadStatusLine, HTTPResponse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import ops
import yaml  # pyyaml; the serialized-data-interface wire format is YAML-encoded

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_TCP_TIMEOUT = 5
_HTTP_TIMEOUT = 10

_VERSION_KEY = "_supported_versions"
_DATA_KEY = "data"

# The published ingress-auth schema defines only v1, and every field name and deep-check
# assumption below is v1's. A future version would have to be read against its own schema.
_SUPPORTED_VERSIONS = ("v1",)

_REQUIRED_FIELDS = ["service", "port"]
_HEADER_FIELDS = ("allowed-request-headers", "allowed-response-headers")

# How Envoy's HTTP ext_authz client maps an authorization response: 5xx is an error (the
# gateway cannot obtain a decision at all), exactly 200 admits the request, and every other
# status is a deny. See Envoy's ext_authz_http_impl.cc.
_SERVER_ERROR_FLOOR = 500
_ALLOW_STATUS = 200

_CROSS_MODEL_HINT = (
    " The relation is cross-model: ingress-auth advertises a bare service name with no"
    " model or namespace, so the provider resolves it inside its own model while the"
    " requirer's service lives in another one. Both applications can report 'active'"
    " while the gateway's authorization path is broken."
)
_CROSS_MODEL_RESOLVED = (
    " The relation is cross-model, so this name resolves to a local alias for the remote"
    " service rather than to the requirer's own service."
)
_PLAINTEXT_HINT = (
    " The ingress-auth payload carries no scheme, so the provider addresses the"
    " authorization service over plaintext HTTP; a service accepting only TLS on this"
    " port fails exactly like this."
)


class IngressAuthValidator(BaseValidator):
    """Validates the ``ingress-auth`` interface from the provider (``provides``) side.

    ``ingress-auth`` is a serialized-data-interface (SDI) relation in which the
    *requirer* advertises an external authorization service (``service`` and ``port``)
    that the provider must place in front of its ingress gateway as an Envoy
    ``ext_authz`` filter. All meaningful relation data therefore travels
    requirer -> provider, and ``self.databag`` (always the remote application's
    databag) only carries a payload when this validator runs on the provider.

    The payload names the authorization service by bare name only, with no host,
    namespace or model, so the provider can resolve it only inside its own model.
    A cross-model relation is therefore only operational if the service name also
    resolves locally, and the deep checks report that explicitly.

    The deep level exercises the advertised authorization service the same way the
    gateway does at request time: it resolves the service, opens a connection and
    asks it for an authorization decision on an unauthenticated request.
    """

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "provides":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        databag = self.databag

        versions_check = _supported_versions_check(databag)
        checks: list[ValidationCheck] = [versions_check]
        if not versions_check.passed:
            return self._fail_result(level, checks)

        payload_check, payload = _decode_payload(databag)
        checks.append(payload_check)
        if not payload_check.passed:
            return self._fail_result(level, checks)

        checks.append(self.validate_schema(_REQUIRED_FIELDS, creds=_stringify(payload)))
        if not checks[-1].passed:
            return self._fail_result(level, checks)

        checks.append(_field_types_check(payload))
        if not checks[-1].passed:
            return self._fail_result(level, checks)

        if level == "deep":
            checks.extend(self._deep_checks(payload))

        return self._make_result(level=level, checks=checks)

    def _deep_checks(self, payload: dict[str, Any]) -> list[ValidationCheck]:
        """Exercise the advertised authorization service as the ingress gateway would."""
        service = str(payload["service"])
        port = int(payload["port"])

        dns_check, host = _dns_check(service, self.charm.model.name, self._is_cross_model())
        checks = [dns_check]
        if not dns_check.passed:
            return checks

        checks.append(_connect_check(host, port))
        if not checks[-1].passed:
            return checks

        probe_check, status, headers = _ext_authz_probe_check(host, port)
        checks.append(probe_check)
        if not probe_check.passed:
            return checks

        checks.append(_auth_decision_check(status, headers, payload.get("allowed-response-headers") or []))
        return checks

    def _is_cross_model(self) -> bool | None:
        """Whether the remote application lives in another model, or None if unknown.

        ``Relation.remote_model`` needs Juju 3.6.2 or later.
        """
        try:
            return self.relation.remote_model.uuid != self.charm.model.uuid
        except (AttributeError, ops.ModelError):
            return None


# ---------------------------------------------------------------------------
# Pure helpers -- SDI wire format decoding
# ---------------------------------------------------------------------------


def _supported_versions_check(databag: dict[str, str]) -> ValidationCheck:
    """Verify the remote completed the SDI version handshake on a version we can read."""
    raw = databag.get(_VERSION_KEY, "")
    if not raw:
        return ValidationCheck(
            name="supported_versions",
            passed=False,
            message=f"Missing '{_VERSION_KEY}' in the remote app databag; the SDI version handshake did not complete.",
        )

    try:
        versions = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return ValidationCheck(
            name="supported_versions",
            passed=False,
            message=f"'{_VERSION_KEY}' is not valid YAML: {exc}",
        )

    if not versions or not isinstance(versions, list) or not all(isinstance(version, str) for version in versions):
        return ValidationCheck(
            name="supported_versions",
            passed=False,
            message=f"'{_VERSION_KEY}' must be a non-empty list of strings, got {versions!r}.",
        )

    # SDI negotiates the highest version both sides advertise, so a superset is fine: the
    # provider only supports v1, which is what will be used.
    usable = [version for version in versions if version in _SUPPORTED_VERSIONS]
    if not usable:
        return ValidationCheck(
            name="supported_versions",
            passed=False,
            message=(
                f"Remote advertises versions {versions}, none of which this validator"
                f" supports ({list(_SUPPORTED_VERSIONS)}); its payload cannot be read as"
                f" {_SUPPORTED_VERSIONS[0]}."
            ),
        )

    return ValidationCheck(
        name="supported_versions",
        passed=True,
        message=f"Remote advertises versions {versions}; validating as {usable[0]}.",
    )


def _decode_payload(databag: dict[str, str]) -> tuple[ValidationCheck, dict[str, Any]]:
    """Decode the SDI payload, supporting both the nested and flat wire formats.

    Nested (the default) puts the whole payload under a single YAML-encoded ``data``
    key. Flat serialises each field directly into its own databag key. Returns a
    (check, payload) tuple; the payload is empty when the check fails.
    """
    if _DATA_KEY in databag:
        try:
            payload = yaml.safe_load(databag[_DATA_KEY])
        except yaml.YAMLError as exc:
            return (
                ValidationCheck(
                    name="payload",
                    passed=False,
                    message=f"'{_DATA_KEY}' is not valid YAML: {exc}",
                ),
                {},
            )
        if not isinstance(payload, dict):
            return (
                ValidationCheck(
                    name="payload",
                    passed=False,
                    message=f"'{_DATA_KEY}' must decode to a mapping, got {type(payload).__name__}.",
                ),
                {},
            )
        if not all(isinstance(key, str) for key in payload):
            return (
                ValidationCheck(
                    name="payload",
                    passed=False,
                    message=f"'{_DATA_KEY}' mapping keys must all be strings.",
                ),
                {},
            )
        return (
            ValidationCheck(
                name="payload",
                passed=True,
                message=f"Decoded nested SDI payload with fields {sorted(payload)}.",
            ),
            payload,
        )

    flat = {key: value for key, value in databag.items() if key != _VERSION_KEY}
    if not flat:
        return (
            ValidationCheck(
                name="payload",
                passed=False,
                message="Remote app databag carries no authorization service data; the requirer published nothing.",
            ),
            {},
        )

    payload = {}
    for key, value in flat.items():
        try:
            payload[key] = yaml.safe_load(value)
        except yaml.YAMLError:
            payload[key] = value
    return (
        ValidationCheck(
            name="payload",
            passed=True,
            message=f"Decoded flat SDI payload with fields {sorted(payload)}.",
        ),
        payload,
    )


def _stringify(payload: dict[str, Any]) -> dict[str, str]:
    """Render a decoded payload as strings so it can feed ``validate_schema``."""
    return {key: "" if value is None else str(value) for key, value in payload.items()}


def _field_types_check(payload: dict[str, Any]) -> ValidationCheck:
    """Verify the payload fields match the ingress-auth v1 schema types."""
    problems: list[str] = []

    service = payload.get("service")
    if not isinstance(service, str) or not service.strip():
        problems.append(f"'service' must be a non-empty string, got {service!r}")

    port = payload.get("port")
    # bool is a subclass of int but is never a valid port.
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        problems.append(f"'port' must be an integer in 1..65535, got {port!r}")

    for field in _HEADER_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            problems.append(f"'{field}' must be a list of strings, got {value!r}")

    if problems:
        return ValidationCheck(name="field_types", passed=False, message="; ".join(problems))
    return ValidationCheck(
        name="field_types",
        passed=True,
        message=f"Authorization service {payload['service']}:{payload['port']} declared with a valid schema.",
    )


# ---------------------------------------------------------------------------
# Deep level checks
# ---------------------------------------------------------------------------


def _dns_check(service: str, model_name: str, cross_model: bool | None) -> tuple[ValidationCheck, str]:
    """Resolve the advertised service at the address the provider programs into the proxy.

    The payload carries a bare service name, so the provider can only resolve it as an
    in-cluster name inside its own model. Only that name is tried: resolving anything
    else would report reachability for an address no request will ever use.
    """
    fqdn = f"{service}.{model_name}.svc.cluster.local"
    try:
        # getaddrinfo, not gethostbyname: the latter is IPv4-only and would report a
        # healthy service on an IPv6-only cluster as unresolvable, even though the
        # connection check below and Envoy both use its AAAA record.
        infos = socket.getaddrinfo(fqdn, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        return (
            ValidationCheck(
                name="auth_service_dns",
                passed=False,
                message=f"Advertised service {service!r} does not resolve as {fqdn} ({exc})."
                + (_CROSS_MODEL_HINT if cross_model else ""),
            ),
            "",
        )

    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        return (
            ValidationCheck(
                name="auth_service_dns",
                passed=False,
                message=f"Advertised service {service!r} resolves as {fqdn} but to no addresses."
                + (_CROSS_MODEL_HINT if cross_model else ""),
            ),
            "",
        )

    suffix = _CROSS_MODEL_RESOLVED if cross_model else ""
    return (
        ValidationCheck(
            name="auth_service_dns",
            passed=True,
            message=f"Advertised service resolves: {fqdn} -> {', '.join(addresses)}.{suffix}",
        ),
        fqdn,
    )


def _connect_check(host: str, port: int) -> ValidationCheck:
    """Open a TCP connection to the authorization service."""
    try:
        with socket.create_connection((host, port), timeout=float(_TCP_TIMEOUT)):
            pass
    except OSError as exc:
        return ValidationCheck(
            name="auth_service_connect",
            passed=False,
            message=f"Authorization service {host}:{port} is not accepting connections: {exc}",
        )
    return ValidationCheck(
        name="auth_service_connect",
        passed=True,
        message=f"TCP reached authorization service {host}:{port}.",
    )


def _ext_authz_probe_check(host: str, port: int) -> tuple[ValidationCheck, int | None, dict[str, str]]:
    """Ask the authorization service to authorize an unauthenticated request.

    This mirrors the check request the ingress gateway issues per inbound request,
    including its plaintext HTTP scheme: probing over TLS would report success for a
    path no request ever takes. Redirects are deliberately not followed, because a
    redirect *is* the authorization decision and must reach the client verbatim.
    """
    url = f"http://{host}:{port}/"
    try:
        status, headers = _http_probe(url)
    except Exception as exc:
        return (
            ValidationCheck(
                name="ext_authz_probe",
                passed=False,
                message=f"Authorization request to {url} failed: {exc}.{_protocol_hint(exc)}",
            ),
            None,
            {},
        )

    if status >= _SERVER_ERROR_FLOOR:
        return (
            ValidationCheck(
                name="ext_authz_probe",
                passed=False,
                message=f"Authorization service returned {status} for {url}; it cannot produce a decision.",
            ),
            status,
            headers,
        )

    return (
        ValidationCheck(
            name="ext_authz_probe",
            passed=True,
            message=f"Authorization service answered {url} with {status}.",
        ),
        status,
        headers,
    )


def _auth_decision_check(
    status: int | None,
    headers: dict[str, str],
    allowed_response_headers: list[str],
) -> ValidationCheck:
    """Verify the authorization decision is one the gateway can actually act on."""
    if status is None:
        return ValidationCheck(
            name="auth_decision",
            passed=False,
            message="No authorization decision was obtained.",
        )

    if status == _ALLOW_STATUS:
        forwarded = [h for h in allowed_response_headers if h.lower() in headers]
        detail = f" Upstream headers present: {forwarded}." if allowed_response_headers else ""
        return ValidationCheck(
            name="auth_decision",
            passed=True,
            message=f"Decision ALLOW ({status}).{detail}",
        )

    if 200 <= status < 300:
        return ValidationCheck(
            name="auth_decision",
            passed=False,
            message=(
                f"Authorization service answered {status}, but ext_authz admits traffic only on"
                f" {_ALLOW_STATUS}, so the gateway denies every request and never forwards"
                f" {allowed_response_headers or 'any declared'} upstream headers. A success status"
                " other than 200 usually means the service targets a proxy that accepts any 2xx."
            ),
        )

    if 300 <= status < 400:
        location = headers.get("location", "")
        if not location:
            return ValidationCheck(
                name="auth_decision",
                passed=False,
                message=(
                    f"Decision was a {status} redirect but no 'Location' header was returned, "
                    "so the gateway cannot start the authentication flow."
                ),
            )
        return ValidationCheck(
            name="auth_decision",
            passed=True,
            message=f"Decision DENY ({status}), redirecting unauthenticated requests to {location!r}.",
        )

    return ValidationCheck(
        name="auth_decision",
        passed=True,
        message=f"Decision DENY ({status}) for an unauthenticated request.",
    )


# ---------------------------------------------------------------------------
# Low-level HTTP helper
# ---------------------------------------------------------------------------


class _NoRedirectHandler(HTTPRedirectHandler):
    """Surface 3xx responses instead of following them."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _http_probe(url: str) -> tuple[int, dict[str, str]]:
    """GET *url* without following redirects, returning (status, lowercased headers)."""
    opener = build_opener(_NoRedirectHandler)
    try:
        response: HTTPResponse
        with opener.open(Request(url), timeout=_HTTP_TIMEOUT) as response:
            return response.status, _lower_headers(response.headers.items())
    except HTTPError as exc:
        # HTTPError is itself a response object; read the decision off it and close it.
        status, headers = exc.code, _lower_headers(exc.headers.items())
        exc.close()
        return status, headers


def _lower_headers(items: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in items}


def _protocol_hint(exc: BaseException) -> str:
    """Flag the failures a TLS-only authorization service produces on a plaintext probe.

    DNS and connectivity are already known good by this point, so a transport-level
    failure here is a protocol mismatch rather than a network fault.
    """
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, (ConnectionResetError, BadStatusLine)):
        return _PLAINTEXT_HINT
    return ""
