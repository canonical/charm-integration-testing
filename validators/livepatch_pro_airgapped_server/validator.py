# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Validator for the livepatch-pro-airgapped-server Juju interface.

The livepatch-pro-airgapped-server interface connects the Pro airgapped
contracts server (provider) to the Canonical Livepatch server (requirer)
in airgapped deployments.

Interface name:  livepatch-pro-airgapped-server
Provider:        pro-airgapped-server  — publishes contracts server address
                 via each unit's own unit databag
Requirer:        canonical-livepatch-server / canonical-livepatch-server-k8s
                 — reads the address and sets LP_CONTRACTS_URL

Provider unit databag fields (written by each pro-airgapped-server unit):
    hostname    Required.  DNS hostname or IP of the contracts server.
    scheme      Optional.  URL scheme: ``http`` (default) or ``https``.
    port        Optional.  TCP port as a decimal string (e.g. ``8484``).

The requirer constructs the contracts URL as
``{scheme}://{hostname}[:{port}]`` and sets ``LP_CONTRACTS_URL`` so the
Livepatch server can validate Pro entitlements without reaching
contracts.canonical.com.
"""

import socket
import urllib.error
import urllib.parse
import urllib.request

import ops

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_VALID_SCHEMES = ("http", "https")
_CONNECT_TIMEOUT_SECS = 5
_HTTP_TIMEOUT_SECS = 10

# Canary HTTP path used at L2.  The Pro contracts server exposes a standard
# ``/v1/`` API root; a bare GET returns a well-formed JSON response that can
# be used to confirm the server is accepting connections.
_CANARY_PATH = "/v1/"


class LivepatchProAirgappedServerValidator(BaseValidator):
    """Validates the livepatch-pro-airgapped-server relation contract.

    Validation is performed from the **requires** role only; the provider side
    writes its address to unit databags which the requirer (Livepatch server)
    reads.
    """

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level == "simple":
            return self._validate_simple()
        if level == "deep":
            return self._validate_deep()
        return self._skipped_result_due_to_level(level)

    # ------------------------------------------------------------------
    # L1 – schema + TCP connectivity
    # ------------------------------------------------------------------

    def _validate_simple(self) -> ValidationResult:
        """L1: Validate required databag fields, field formats, and TCP reach."""
        checks: list[ValidationCheck] = []

        if not self.relation_exists():
            return self._error_result("simple", f"No remote application on relation '{self.endpoint}'.")

        unit_dbs = _unit_databags(self.relation)
        unit_check = _check_units_published(unit_dbs)
        checks.append(unit_check)
        if not unit_check.passed:
            return self._make_result(level="simple", checks=checks)

        fields = _first_populated_unit(unit_dbs)
        hostname = fields.get("hostname", "")
        scheme = fields.get("scheme", "http")
        port_raw = fields.get("port", "")

        scheme_check = _check_scheme(scheme)
        checks.append(scheme_check)
        if not scheme_check.passed:
            return self._make_result(level="simple", checks=checks)

        port_check, port = _check_port(port_raw)
        checks.append(port_check)
        if not port_check.passed:
            return self._make_result(level="simple", checks=checks)

        tcp_check = _check_tcp(hostname, port, scheme)
        checks.append(tcp_check)

        return self._make_result(level="simple", checks=checks)

    # ------------------------------------------------------------------
    # L2 – HTTP canary interaction
    # ------------------------------------------------------------------

    def _validate_deep(self) -> ValidationResult:
        """L2: All L1 checks + HTTP canary against the contracts API root."""
        checks: list[ValidationCheck] = []

        if not self.relation_exists():
            return self._error_result("deep", f"No remote application on relation '{self.endpoint}'.")

        unit_dbs = _unit_databags(self.relation)
        unit_check = _check_units_published(unit_dbs)
        checks.append(unit_check)
        if not unit_check.passed:
            return self._make_result(level="deep", checks=checks)

        fields = _first_populated_unit(unit_dbs)
        hostname = fields.get("hostname", "")
        scheme = fields.get("scheme", "http")
        port_raw = fields.get("port", "")

        scheme_check = _check_scheme(scheme)
        checks.append(scheme_check)
        if not scheme_check.passed:
            return self._make_result(level="deep", checks=checks)

        port_check, port = _check_port(port_raw)
        checks.append(port_check)
        if not port_check.passed:
            return self._make_result(level="deep", checks=checks)

        tcp_check = _check_tcp(hostname, port, scheme)
        checks.append(tcp_check)
        if not tcp_check.passed:
            return self._make_result(level="deep", checks=checks)

        base_url = _build_url(scheme, hostname, port)
        http_check = _check_http_canary(base_url)
        checks.append(http_check)

        return self._make_result(level="deep", checks=checks)


# ---------------------------------------------------------------------------
# Unit-databag helpers
# ---------------------------------------------------------------------------


def _unit_databags(relation: ops.Relation) -> list[dict[str, str]]:
    """Return a list of non-empty unit databag dicts from remote units.

    The livepatch-pro-airgapped-server provider writes server address fields
    (``hostname``, ``scheme``, ``port``) to each provider unit's own unit
    databag rather than the application databag.
    """
    result: list[dict[str, str]] = []
    for unit in relation.units:
        data = relation.data.get(unit)
        if data:
            result.append(dict(data))
    return result


def _first_populated_unit(unit_dbs: list[dict[str, str]]) -> dict[str, str]:
    """Return the first unit databag that contains a ``hostname`` value."""
    for db in unit_dbs:
        if db.get("hostname"):
            return db
    return {}


# ---------------------------------------------------------------------------
# Pure validation helpers
# ---------------------------------------------------------------------------


def _check_units_published(unit_dbs: list[dict[str, str]]) -> ValidationCheck:
    """Check that at least one remote unit has published a non-empty hostname.

    An empty or missing ``hostname`` means the pro-airgapped-server has not
    finished initialising.  Remediation: wait for the provider application to
    become active/idle and verify the relation is established.
    """
    if not unit_dbs:
        return ValidationCheck(
            name="unit_data",
            passed=False,
            message=(
                "No unit data found on the relation. "
                "Ensure pro-airgapped-server is deployed and the integration is established."
            ),
        )
    for db in unit_dbs:
        if db.get("hostname"):
            return ValidationCheck(
                name="unit_data",
                passed=True,
                message=f"Provider unit published hostname '{db['hostname']}'.",
            )
    return ValidationCheck(
        name="unit_data",
        passed=False,
        message=(
            "'hostname' is absent or empty in all provider unit databags. "
            "Remediation: wait for pro-airgapped-server to complete initialisation "
            "and write its address to the relation data."
        ),
    )


def _check_scheme(scheme: str) -> ValidationCheck:
    """Validate that the ``scheme`` field is ``http`` or ``https``."""
    if scheme in _VALID_SCHEMES:
        return ValidationCheck(name="scheme", passed=True, message=f"Scheme '{scheme}' is valid.")
    return ValidationCheck(
        name="scheme",
        passed=False,
        message=(
            f"'scheme' value '{scheme}' is not a supported URL scheme. "
            f"Expected one of: {', '.join(_VALID_SCHEMES)}. "
            "Remediation: check that pro-airgapped-server is configured correctly."
        ),
    )


def _check_port(port_raw: str) -> tuple[ValidationCheck, int | None]:
    """Validate the optional ``port`` field and return (check, parsed_port).

    When ``port_raw`` is empty the field is absent and ``None`` is returned
    as the port (callers should omit the port from the URL).
    """
    if not port_raw:
        return (
            ValidationCheck(name="port", passed=True, message="No explicit port; using scheme default."),
            None,
        )
    try:
        port = int(port_raw)
        if not 1 <= port <= 65535:
            raise ValueError(f"Port {port} is outside the valid range 1–65535.")
    except ValueError as exc:
        return (
            ValidationCheck(
                name="port",
                passed=False,
                message=(
                    f"'port' value '{port_raw}' is not a valid TCP port number: {exc}. "
                    "Expected a decimal integer between 1 and 65535."
                ),
            ),
            None,
        )
    return ValidationCheck(name="port", passed=True, message=f"Port {port} is a valid TCP port."), port


def _check_tcp(hostname: str, port: int | None, scheme: str = "http") -> ValidationCheck:
    """Attempt a TCP connection to hostname:port to verify network reachability.

    When *port* is None the default port for the given *scheme* is probed
    (80 for ``http``, 443 for ``https``).
    """
    target_port = port if port is not None else (443 if scheme == "https" else 80)
    try:
        with socket.create_connection((hostname, target_port), timeout=_CONNECT_TIMEOUT_SECS):
            pass
        return ValidationCheck(
            name="tcp_connect",
            passed=True,
            message=f"TCP connection to {hostname}:{target_port} succeeded.",
        )
    except OSError as exc:
        return ValidationCheck(
            name="tcp_connect",
            passed=False,
            message=(
                f"Cannot reach {hostname}:{target_port} — {exc}. "
                "Remediation: verify that pro-airgapped-server is running, the "
                "network path between units is open, and the hostname resolves correctly."
            ),
        )


def _build_url(scheme: str, hostname: str, port: int | None) -> str:
    """Construct the contracts base URL from validated fields.

    IPv6 literal hostnames are wrapped in brackets so the resulting URL netloc
    is valid (e.g. ``http://[2001:db8::1]``).
    """
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    netloc = host if port is None else f"{host}:{port}"
    return urllib.parse.urlunparse((scheme, netloc, "", "", "", ""))


def _check_http_canary(base_url: str) -> ValidationCheck:
    """GET ``{base_url}/v1/`` as a canary interaction.

    The Pro contracts API root returns a JSON response for valid requests.
    A 4xx response (e.g. 401 Unauthorized) is still a successful *connectivity*
    canary because it confirms the server is accepting HTTP connections; only
    network-level errors are treated as failures.
    """
    canary_url = base_url.rstrip("/") + _CANARY_PATH
    try:
        req = urllib.request.Request(canary_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECS) as resp:  # noqa: S310  # nosec B310
            status_code = resp.status
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        # 4xx / 5xx: server is reachable, just rejecting the request.
        # Treat 4xx as a successful canary (server responds); 5xx as failure.
        if exc.code < 500:
            return ValidationCheck(
                name="http_canary",
                passed=True,
                message=(
                    f"HTTP {exc.code} from {canary_url} — server is reachable "
                    "(authentication or routing required, which is expected for this endpoint)."
                ),
            )
        return ValidationCheck(
            name="http_canary",
            passed=False,
            message=(
                f"HTTP {exc.code} server error from {canary_url}. "
                "Remediation: check pro-airgapped-server application logs for errors."
            ),
        )
    except urllib.error.URLError as exc:
        return ValidationCheck(
            name="http_canary",
            passed=False,
            message=(
                f"Cannot reach contracts API at {canary_url}: {exc.reason}. "
                "Remediation: verify the pro-airgapped-server service is running "
                "and the URL is reachable from this unit."
            ),
        )
    except Exception as exc:
        return ValidationCheck(
            name="http_canary",
            passed=False,
            message=f"Unexpected error reaching {canary_url}: {exc}.",
        )

    return ValidationCheck(
        name="http_canary",
        passed=True,
        message=(
            f"HTTP {status_code} from {canary_url} "
            f"(Content-Type: {content_type or 'not set'}). "
            "Contracts API is reachable."
        ),
    )
