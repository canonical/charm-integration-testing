# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Validator for the trino_client interface.

The provider (a Trino coordinator, e.g. `trino-k8s`) publishes a `discovery-uri`
field on the relation databag (e.g. `http://host:8080`) from which the host and
port are parsed. Authentication is optional: if the provider config points
`user-secret-id` at a Juju secret, it is resolved for `username`/`password`;
otherwise the validator connects anonymously, which Trino permits unless a
password authenticator is configured.
"""

import urllib.parse
from typing import Any

import trino
import trino.dbapi

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_DEFAULT_PORT = 8080
_CONNECT_TIMEOUT_S = 10
_ANONYMOUS_USER = "charm-integration-testing-validator"


class TrinoClientValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level == "simple":
            return self._validate_simple()
        elif level == "deep":
            return self._validate_deep()
        else:
            return self._skipped_result_due_to_level(level)

    def _validate_simple(self) -> ValidationResult:
        """L1: Connectivity - connect via trino-python-client and confirm the cluster is reachable."""
        checks: list[ValidationCheck] = []

        error_result = self._check_relation_exists("simple")
        if error_result:
            return error_result

        connection_info = self._prepare_connection(checks)
        if connection_info is None:
            return self._make_result(level="simple", checks=checks)
        host, port, http_scheme = connection_info

        conn = None
        try:
            creds = self._resolve_credentials()
            conn = self._connect(host, port, http_scheme, creds)
            with conn.cursor() as cur:
                cur.execute("SHOW CATALOGS")
                catalogs = cur.fetchall()
            checks.append(
                ValidationCheck(
                    name="connect",
                    passed=True,
                    message=f"Connected to {host}:{port}. Found {len(catalogs)} catalog(s).",
                )
            )
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
        finally:
            self._close(conn)

        return self._make_result(level="simple", checks=checks)

    def _validate_deep(self) -> ValidationResult:
        """L2: Canary - execute SELECT 1 to confirm query execution works end-to-end."""
        checks: list[ValidationCheck] = []

        error_result = self._check_relation_exists("deep")
        if error_result:
            return error_result

        connection_info = self._prepare_connection(checks)
        if connection_info is None:
            return self._make_result(level="deep", checks=checks)
        host, port, http_scheme = connection_info

        conn = None
        try:
            creds = self._resolve_credentials()
            conn = self._connect(host, port, http_scheme, creds)
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
            if row is not None and row[0] == 1:
                checks.append(ValidationCheck(name="query", passed=True, message="SELECT 1 OK."))
            else:
                checks.append(ValidationCheck(name="query", passed=False, message=f"Unexpected result: {row!r}."))
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
        finally:
            self._close(conn)

        return self._make_result(level="deep", checks=checks)

    def _prepare_connection(self, checks: list[ValidationCheck]) -> tuple[str, int, str] | None:
        """Run schema and discovery-uri parsing shared by both levels.

        Appends checks to *checks* in place. Returns (host, port, http_scheme) on success, or
        None if either check failed, signalling the caller to stop and return
        immediately.
        """
        schema_check = self.validate_schema(["discovery-uri"])
        checks.append(schema_check)
        if not schema_check.passed:
            return None

        host, port, http_scheme, uri_check = self._parse_discovery_uri(self.databag["discovery-uri"])
        checks.append(uri_check)
        if not uri_check.passed or host is None or port is None or http_scheme is None:
            return None

        return host, port, http_scheme

    def _check_relation_exists(self, level: ValidationLevel) -> ValidationResult | None:
        """Return an ERROR result if the remote app is absent, else None."""
        if not self.relation_exists():
            return self._error_result(level=level, error=f"No remote application on relation '{self.endpoint}'.")
        return None

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve optional credentials from the `user-secret-id` Juju secret."""
        return self.resolve_secret("user-secret-id", "username", "password")

    def _parse_discovery_uri(self, uri: str) -> tuple[str | None, int | None, str | None, ValidationCheck]:
        """Parse scheme/host/port from the coordinator's `discovery-uri` (e.g. `http://host:8080`)."""
        try:
            parsed = urllib.parse.urlsplit(uri)
            scheme = parsed.scheme.lower()
            if scheme not in {"http", "https"}:
                raise ValueError(f"unsupported scheme '{scheme or '<missing>'}'")
            host = parsed.hostname
            if not host:
                raise ValueError("discovery-uri has no hostname")
            port = parsed.port or _DEFAULT_PORT
        except Exception as exc:
            return (
                None,
                None,
                None,
                ValidationCheck(name="discovery_uri", passed=False, message=f"Could not parse discovery-uri: {exc}"),
            )
        return (
            host,
            port,
            scheme,
            ValidationCheck(
                name="discovery_uri",
                passed=True,
                message=f"Parsed scheme='{scheme}', host='{host}', port={port}.",
            ),
        )

    def _connect(self, host: str, port: int, http_scheme: str, creds: dict[str, str]) -> "trino.dbapi.Connection":
        """Open a trino-python-client connection, authenticated if credentials are present."""
        username = creds.get("username")
        password = creds.get("password")
        kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "http_scheme": http_scheme,
            "user": username or _ANONYMOUS_USER,
            "request_timeout": _CONNECT_TIMEOUT_S,
        }
        if username and password:
            kwargs["auth"] = trino.auth.BasicAuthentication(username, password)
        return trino.dbapi.connect(**kwargs)  # type: ignore[no-any-return,no-untyped-call]

    def _close(self, conn: "trino.dbapi.Connection | None") -> None:
        """Close a trino-python-client connection, tolerating its untyped signature."""
        if conn is not None:
            conn.close()  # type: ignore[no-untyped-call]
