# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import socket
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_HTTP_TIMEOUT = 5
_HTTP_GET_TIMEOUT = 5


class HttpInterfaceValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if self.relation.app is None:
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        unit_infos, collection_errors = _collect_unit_infos(self.relation)

        schema_check = _schema_check(unit_infos, collection_errors)
        checks: list[ValidationCheck] = [schema_check]
        if not schema_check.passed:
            return self._fail_result(level, checks)

        checks.append(_connectivity_check(unit_infos))
        if not checks[-1].passed:
            return self._fail_result(level, checks)

        if level == "deep":
            checks.extend(_http_probe_checks(unit_infos))

        return self._make_result(level=level, checks=checks)


# ---------------------------------------------------------------------------
# Pure helpers — endpoint collection
# ---------------------------------------------------------------------------


def _collect_unit_infos(
    relation: Any,
) -> tuple[list[dict[str, str]], list[str]]:
    """Collect and parse endpoint info from provider unit databags.

    The ``http`` interface stores per-unit data in the unit-level databag::

        relation.data[unit]["hostname"] = "<ip-or-hostname>"
        relation.data[unit]["port"]     = "<port-number>"

    Malformed or missing entries are returned as errors so ``_schema_check``
    can surface every bad databag in one pass.
    """
    infos: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()

    for unit in sorted(relation.units, key=lambda u: getattr(u, "name", repr(u))):
        unit_name = getattr(unit, "name", repr(unit))
        unit_data = dict(relation.data.get(unit, {}))

        hostname = unit_data.get("hostname", "").strip()
        port = unit_data.get("port", "").strip()

        if not hostname:
            errors.append(f"Unit {unit_name!r}: missing 'hostname' in databag")
            continue
        if not port:
            errors.append(f"Unit {unit_name!r}: missing 'port' in databag")
            continue

        key = (hostname, port)
        if key in seen:
            continue  # deduplicate identical endpoints from scaled-out providers
        seen.add(key)
        infos.append({"hostname": hostname, "port": port, "unit": unit_name})

    return infos, errors


# ---------------------------------------------------------------------------
# Pure helpers — schema validation
# ---------------------------------------------------------------------------


def _schema_check(
    unit_infos: list[dict[str, str]],
    collection_errors: list[str],
) -> ValidationCheck:
    """Validate structure and field types of all advertised unit endpoints.

    Checks applied per endpoint:

    * ``hostname`` is a non-empty string.
    * ``port`` is a string representation of an integer in the valid TCP range
      (1–65535).
    """
    if not unit_infos and not collection_errors:
        return ValidationCheck(
            name="schema",
            passed=False,
            message="No endpoint data found in provider unit databags.",
        )

    errors: list[str] = list(collection_errors)
    for info in unit_infos:
        hostname = info["hostname"]
        port_str = info["port"]
        unit_name = info["unit"]

        if not hostname:
            errors.append(f"Unit {unit_name!r}: 'hostname' is empty")
            continue

        try:
            port_int = int(port_str)
        except ValueError:
            errors.append(f"Unit {unit_name!r}: 'port' is not a valid integer: {port_str!r}")
            continue

        if not (1 <= port_int <= 65535):
            errors.append(f"Unit {unit_name!r}: 'port' {port_int} is out of valid range (1–65535)")

    if errors:
        return ValidationCheck(name="schema", passed=False, message="; ".join(errors))
    return ValidationCheck(
        name="schema",
        passed=True,
        message=f"Validated {len(unit_infos)} endpoint(s).",
    )


# ---------------------------------------------------------------------------
# Pure helpers — L1 TCP connectivity
# ---------------------------------------------------------------------------


def _connectivity_check(unit_infos: list[dict[str, str]]) -> ValidationCheck:
    """TCP-ping every provider endpoint; return a single pass/fail check."""
    errors: list[str] = []
    for info in unit_infos:
        host = info["hostname"]
        port = int(info["port"])
        try:
            _tcp_ping(host, port)
        except Exception as exc:
            errors.append(f"{host}:{port}: {exc}")

    if errors:
        return ValidationCheck(name="connect", passed=False, message="; ".join(errors))
    return ValidationCheck(
        name="connect",
        passed=True,
        message=f"TCP reached {len(unit_infos)} endpoint(s).",
    )


# ---------------------------------------------------------------------------
# Pure helpers — L2 HTTP canary probe
# ---------------------------------------------------------------------------


def _http_probe_checks(unit_infos: list[dict[str, str]]) -> list[ValidationCheck]:
    """Issue an HTTP GET to each endpoint and verify a meaningful HTTP response.

    A response with any HTTP status code (1xx–5xx) counts as PASS because it
    proves the remote is speaking HTTP.  Only a failure to open the connection
    at the HTTP layer (e.g. connection refused, TLS handshake error, or
    non-HTTP data) is treated as FAIL.
    """
    checks: list[ValidationCheck] = []
    for info in unit_infos:
        host = info["hostname"]
        port = int(info["port"])
        check_name = f"http_probe[{host}:{port}]"
        url = f"http://{host}:{port}/"
        try:
            req = Request(url)  # nosec B310 - scheme is always http://
            with urlopen(req, timeout=_HTTP_GET_TIMEOUT) as resp:  # nosec B310
                status = resp.status
            checks.append(
                ValidationCheck(
                    name=check_name,
                    passed=True,
                    message=f"HTTP GET {url} → {status}.",
                )
            )
        except Exception as exc:
            # urllib raises HTTPError for 4xx/5xx — those still prove HTTP is running
            if isinstance(exc, HTTPError):
                checks.append(
                    ValidationCheck(
                        name=check_name,
                        passed=True,
                        message=f"HTTP GET {url} → {exc.code} (HTTP service reachable).",
                    )
                )
            else:
                checks.append(
                    ValidationCheck(
                        name=check_name,
                        passed=False,
                        message=f"HTTP GET {url} failed: {exc}",
                    )
                )
    return checks


# ---------------------------------------------------------------------------
# Low-level network helpers
# ---------------------------------------------------------------------------


def _tcp_ping(host: str, port: int, timeout: float = float(_HTTP_TIMEOUT)) -> None:
    """Open a TCP connection to *host*:*port* and immediately close it."""
    with socket.create_connection((host, port), timeout=timeout):
        pass
