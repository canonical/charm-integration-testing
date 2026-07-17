# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import base64
import json
import uuid
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import ops

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_HTTP_TIMEOUT = 5

# Grafana's HTTP API always listens on this port inside the workload container;
# the validator runs colocated with the Grafana unit it is checking.
_GRAFANA_BASE_URL = "http://localhost:3000"

# Well-known Juju secret label used by Grafana-family charms to publish their own
# admin credentials (owned by the Grafana application itself, not the relation
# being validated). Falls back to no admin auth if the secret is absent, since not
# every Grafana-compatible charm is guaranteed to use this convention.
_ADMIN_SECRET_LABEL = "admin-password"  # nosec B105
_DEFAULT_ADMIN_USER = "admin"

# Datasource types natively supported by Grafana's provisioning API. Charms may
# still advertise other values (custom/community plugins), so this is used for
# an informational check rather than a hard failure.
_KNOWN_DATASOURCE_TYPES = frozenset(
    {
        "prometheus",
        "loki",
        "tempo",
        "mimir",
        "alertmanager",
        "elasticsearch",
        "influxdb",
        "graphite",
        "jaeger",
        "zipkin",
        "cloudwatch",
        "postgres",
        "mysql",
    }
)

_REQUIRED_SOURCE_DATA_FIELDS = ("model", "model_uuid", "application", "type")


class GrafanaDatasourceValidator(BaseValidator):
    """Validates the ``grafana_datasource`` interface from the Grafana (requires) side.

    The provider (datasource) charm publishes its connection details in its
    application databag (``grafana_source_data`` and ``grafana_source_app_host``)
    and/or per-unit databags (``grafana_source_host``). This validator reads that
    data through ``self.databag`` -- which always resolves to the remote
    application's databag -- and, at the deep level, exercises Grafana's own HTTP
    API to prove the datasource is actually usable.
    """

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        checks: list[ValidationCheck] = []

        schema_check, source_data = _parse_source_data(self.databag)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._fail_result(level, checks)

        type_check = _validate_type(source_data)
        checks.append(type_check)
        if not type_check.passed:
            return self._fail_result(level, checks)
        url_check, url = _extract_datasource_url(self.databag, self.relation)
        checks.append(url_check)
        if not url_check.passed:
            return self._fail_result(level, checks)

        auth_check, auth = _validate_auth_fields(source_data)
        checks.append(auth_check)
        if not auth_check.passed:
            return self._fail_result(level, checks)

        health_check = _grafana_health_check()
        checks.append(health_check)
        if not health_check.passed:
            return self._fail_result(level, checks)

        if level == "deep":
            admin_headers = _resolve_admin_auth_headers(self.charm)
            checks.extend(_deep_checks(source_data, url, auth, admin_headers))

        return self._make_result(level=level, checks=checks)


# ---------------------------------------------------------------------------
# Pure helpers — databag parsing
# ---------------------------------------------------------------------------


def _parse_source_data(databag: dict[str, str]) -> tuple[ValidationCheck, dict[str, Any]]:
    """Parse and validate the 'grafana_source_data' JSON blob.

    Returns (check, source_data). On failure, source_data is an empty dict.
    """
    raw = databag.get("grafana_source_data", "")
    if not raw:
        return (
            ValidationCheck(name="schema", passed=False, message="Missing required field: 'grafana_source_data'"),
            {},
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return (
            ValidationCheck(name="schema", passed=False, message=f"'grafana_source_data' is not valid JSON: {exc}"),
            {},
        )

    if not isinstance(parsed, dict):
        return (
            ValidationCheck(
                name="schema",
                passed=False,
                message=f"'grafana_source_data' must be a JSON object, got {type(parsed).__name__}",
            ),
            {},
        )

    missing = [f for f in _REQUIRED_SOURCE_DATA_FIELDS if not parsed.get(f)]
    if missing:
        return (
            ValidationCheck(
                name="schema",
                passed=False,
                message=f"'grafana_source_data' missing required fields: {', '.join(missing)}",
            ),
            {},
        )

    return ValidationCheck(name="schema", passed=True, message="OK"), parsed


def _validate_type(source_data: dict[str, Any]) -> ValidationCheck:
    """Verify 'type' is a non-empty string and, if unrecognised, warn (non-fatal)."""
    datasource_type = source_data.get("type", "")
    if not isinstance(datasource_type, str) or not datasource_type:
        return ValidationCheck(name="datasource_type", passed=False, message="'type' must be a non-empty string.")

    if datasource_type not in _KNOWN_DATASOURCE_TYPES:
        return ValidationCheck(
            name="datasource_type",
            passed=True,
            message=f"'type' is {datasource_type!r}, which is not a well-known Grafana datasource type.",
        )

    return ValidationCheck(name="datasource_type", passed=True, message=f"OK ({datasource_type!r})")


def _extract_datasource_url(databag: dict[str, str], relation: Any) -> tuple[ValidationCheck, str]:
    """Resolve the datasource URL from the app-level or per-unit databag.

    Prefers the application-level 'grafana_source_app_host' (stable, load-balanced
    address); falls back to the first unit's 'grafana_source_host' otherwise.
    Returns (check, url). On failure, url is an empty string.
    """
    url = databag.get("grafana_source_app_host", "").strip()
    if not url:
        for unit in sorted(relation.units, key=lambda u: getattr(u, "name", repr(u))):
            candidate = relation.data[unit].get("grafana_source_host", "").strip()
            if candidate:
                url = candidate
                break

    if not url:
        return (
            ValidationCheck(
                name="url",
                passed=False,
                message="No datasource URL found ('grafana_source_app_host' or 'grafana_source_host').",
            ),
            "",
        )

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return ValidationCheck(name="url", passed=False, message=f"Failed to parse URL {url!r}: {exc}"), ""

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return (
            ValidationCheck(name="url", passed=False, message=f"URL {url!r} is not a well-formed http(s) URL."),
            "",
        )

    return ValidationCheck(name="url", passed=True, message=f"OK ({url!r})"), url


def _validate_auth_fields(source_data: dict[str, Any]) -> tuple[ValidationCheck, dict[str, Any]]:
    """Verify basic-auth fields are consistent when 'basicAuth' is enabled.

    Returns (check, auth) where auth is a dict of the fields to use when
    registering the datasource with Grafana ('basicAuth', 'basicAuthUser',
    'basicAuthPassword'), or an empty dict when basic auth is not configured.
    """
    extra_fields = source_data.get("extra_fields") or {}
    secure_extra_fields = source_data.get("secure_extra_fields") or {}
    if not isinstance(extra_fields, dict) or not isinstance(secure_extra_fields, dict):
        return (
            ValidationCheck(
                name="auth",
                passed=False,
                message="'extra_fields'/'secure_extra_fields' must be JSON objects.",
            ),
            {},
        )

    if not extra_fields.get("basicAuth"):
        return ValidationCheck(name="auth", passed=True, message="Basic auth not configured."), {}

    missing = []
    if not extra_fields.get("basicAuthUser"):
        missing.append("extra_fields.basicAuthUser")
    if not secure_extra_fields.get("basicAuthPassword"):
        missing.append("secure_extra_fields.basicAuthPassword")
    if missing:
        return (
            ValidationCheck(name="auth", passed=False, message=f"basicAuth enabled but missing: {', '.join(missing)}"),
            {},
        )

    return (
        ValidationCheck(name="auth", passed=True, message="Basic auth fields present."),
        {
            "basicAuth": True,
            "basicAuthUser": extra_fields["basicAuthUser"],
            "basicAuthPassword": secure_extra_fields["basicAuthPassword"],
        },
    )


# ---------------------------------------------------------------------------
# Grafana HTTP API helpers
# ---------------------------------------------------------------------------


def _grafana_request(
    method: str, path: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    """Issue an HTTP request against Grafana's local API; return (status, decoded_json_body)."""
    data = json.dumps(body).encode() if body is not None else None
    req = Request(f"{_GRAFANA_BASE_URL}{path}", data=data, method=method)  # nosec B310 - fixed local URL
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
            raw = resp.read()
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {}
    except HTTPError as exc:
        raw = exc.read()
        exc.close()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {}


def _resolve_admin_auth_headers(charm: ops.CharmBase) -> dict[str, str]:
    """Best-effort resolution of Grafana admin credentials for write operations.

    Looks up a Juju secret owned by the Grafana application under the
    well-known ``admin-password`` label. Returns an empty dict (no auth
    header) if the secret is absent, since not every Grafana-compatible
    charm is guaranteed to publish credentials this way.
    """
    try:
        content = charm.model.get_secret(label=_ADMIN_SECRET_LABEL).get_content()
    except Exception:
        return {}

    password = content.get("password")
    if not password:
        return {}
    username = content.get("username", _DEFAULT_ADMIN_USER)
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _grafana_health_check() -> ValidationCheck:
    """L1: HTTP GET Grafana's /api/health endpoint to confirm it is reachable."""
    try:
        status, payload = _grafana_request("GET", "/api/health")
    except Exception as exc:
        return ValidationCheck(name="grafana_health", passed=False, message=f"GET /api/health failed: {exc}")

    if status != 200:
        return ValidationCheck(
            name="grafana_health", passed=False, message=f"GET /api/health -> HTTP {status}: {payload}"
        )
    return ValidationCheck(name="grafana_health", passed=True, message=f"Grafana reachable: {payload}")


def _deep_checks(
    source_data: dict[str, Any], url: str, auth: dict[str, Any], admin_headers: dict[str, str]
) -> list[ValidationCheck]:
    """L2: register the datasource with Grafana and verify its health endpoint."""
    checks: list[ValidationCheck] = []

    register_check, uid = _register_datasource(source_data, url, auth, admin_headers)
    checks.append(register_check)
    if not register_check.passed or not uid:
        return checks

    try:
        checks.append(_datasource_health_check(uid, admin_headers))
    finally:
        checks.append(_delete_datasource(uid, admin_headers))

    return checks


def _register_datasource(
    source_data: dict[str, Any], url: str, auth: dict[str, Any], admin_headers: dict[str, str]
) -> tuple[ValidationCheck, str]:
    """POST /api/datasources to register a canary datasource; return (check, uid)."""
    name = f"validator-canary-{source_data['application']}-{uuid.uuid4().hex[:8]}"
    body: dict[str, Any] = {
        "name": name,
        "type": source_data["type"],
        "url": url,
        "access": "proxy",
    }
    body.update(auth)

    try:
        status, payload = _grafana_request("POST", "/api/datasources", body=body, headers=admin_headers)
    except Exception as exc:
        return ValidationCheck(name="register_datasource", passed=False, message=f"Registration failed: {exc}"), ""

    if status not in (200, 201):
        return (
            ValidationCheck(
                name="register_datasource",
                passed=False,
                message=f"POST /api/datasources -> HTTP {status}: {payload}",
            ),
            "",
        )

    uid = payload.get("datasource", {}).get("uid") or payload.get("uid", "")
    if not uid:
        return (
            ValidationCheck(
                name="register_datasource",
                passed=False,
                message=f"Registration response did not include a 'uid': {payload}",
            ),
            "",
        )

    return ValidationCheck(name="register_datasource", passed=True, message=f"Registered datasource uid={uid!r}"), uid


def _datasource_health_check(uid: str, admin_headers: dict[str, str]) -> ValidationCheck:
    """GET /api/datasources/uid/<uid>/health and verify status 'OK'."""
    try:
        status, payload = _grafana_request("GET", f"/api/datasources/uid/{uid}/health", headers=admin_headers)
    except Exception as exc:
        return ValidationCheck(name="datasource_health", passed=False, message=f"Health check failed: {exc}")

    reported_status = str(payload.get("status", "")).upper()
    if status != 200 or reported_status != "OK":
        return ValidationCheck(
            name="datasource_health",
            passed=False,
            message=f"GET /api/datasources/uid/{uid}/health -> HTTP {status}, status={reported_status!r}: {payload}",
        )
    return ValidationCheck(name="datasource_health", passed=True, message=f"Datasource health OK: {payload}")


def _delete_datasource(uid: str, admin_headers: dict[str, str]) -> ValidationCheck:
    """DELETE the canary datasource created during deep validation."""
    try:
        status, payload = _grafana_request("DELETE", f"/api/datasources/uid/{uid}", headers=admin_headers)
    except Exception as exc:
        return ValidationCheck(name="cleanup_datasource", passed=False, message=f"Cleanup failed: {exc}")

    if status != 200:
        return ValidationCheck(
            name="cleanup_datasource",
            passed=False,
            message=f"DELETE /api/datasources/uid/{uid} -> HTTP {status}: {payload}",
        )
    return ValidationCheck(name="cleanup_datasource", passed=True, message="Canary datasource removed.")
