# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Validator for the trino_catalog interface."""

import json
import urllib.parse
from dataclasses import dataclass
from typing import Any

import trino
import trino.dbapi

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

_DEFAULT_HTTP_PORT = 8080
_DEFAULT_HTTPS_PORT = 443
_REQUEST_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class TrinoConnectionInfo:
    host: str
    port: int
    http_scheme: str


class TrinoCatalogValidator(BaseValidator):
    """Validate Trino catalog connection information published to a requirer."""

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        checks: list[ValidationCheck] = []
        prepared = self._prepare_relation_data(checks)
        if prepared is None:
            return self._make_result(level=level, checks=checks)

        connection_info, catalogs, credentials = prepared
        if level == "simple":
            checks.append(self._check_connectivity(connection_info, credentials))
            return self._make_result(level=level, checks=checks)

        checks.append(self._query_catalogs(connection_info, catalogs, credentials))
        return self._make_result(level=level, checks=checks)

    def _prepare_relation_data(
        self, checks: list[ValidationCheck]
    ) -> tuple[TrinoConnectionInfo, list[str], dict[str, str]] | None:
        schema_check = self.validate_schema(["trino_url", "trino_catalogs", "trino_credentials_secret_id"])
        checks.append(schema_check)
        if not schema_check.passed:
            return None

        connection_info, url_check = _parse_trino_url(self.databag["trino_url"])
        checks.append(url_check)
        if connection_info is None:
            return None

        catalogs, catalogs_check = _parse_catalogs(self.databag["trino_catalogs"])
        checks.append(catalogs_check)
        if catalogs is None:
            return None

        try:
            credentials = self.resolve_secret("trino_credentials_secret_id", "username", "password")
        except Exception as exc:
            checks.append(
                ValidationCheck(
                    name="credentials",
                    passed=False,
                    message=f"Could not resolve Trino credentials: {exc}",
                )
            )
            return None

        credentials_check = self.validate_schema(["username", "password"], data=credentials)
        credentials_check.name = "credentials"
        checks.append(credentials_check)
        if not credentials_check.passed:
            return None

        return connection_info, catalogs, credentials

    def _check_connectivity(
        self,
        connection_info: TrinoConnectionInfo,
        credentials: dict[str, str],
    ) -> ValidationCheck:
        connection: trino.dbapi.Connection | None = None
        try:
            connection = _connect(connection_info, credentials)
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                row = cursor.fetchone()
            if row is None or row[0] != 1:
                return ValidationCheck(
                    name="connectivity",
                    passed=False,
                    message=f"Unexpected SELECT 1 result: {row!r}.",
                )
            return ValidationCheck(
                name="connectivity",
                passed=True,
                message=(
                    "Authenticated SELECT 1 succeeded."
                    if connection_info.http_scheme == "https"
                    else (
                        "SELECT 1 succeeded over HTTP using the published username; "
                        "Trino does not authenticate passwords on insecure HTTP connections."
                    )
                ),
            )
        except Exception as exc:
            return ValidationCheck(
                name="connectivity",
                passed=False,
                message=f"Could not query Trino: {exc}",
            )
        finally:
            if connection is not None:
                connection.close()  # type: ignore[no-untyped-call]

    def _query_catalogs(
        self,
        connection_info: TrinoConnectionInfo,
        advertised_catalogs: list[str],
        credentials: dict[str, str],
    ) -> ValidationCheck:
        connection: trino.dbapi.Connection | None = None
        try:
            connection = _connect(connection_info, credentials)
            with connection.cursor() as cursor:
                cursor.execute("SHOW CATALOGS")
                rows = cursor.fetchall()
            available_catalogs = {str(row[0]) for row in rows}
            missing = sorted(set(advertised_catalogs) - available_catalogs)
            if missing:
                return ValidationCheck(
                    name="catalog_query",
                    passed=False,
                    message=f"Advertised catalogs unavailable in Trino: {', '.join(missing)}",
                )
            return ValidationCheck(
                name="catalog_query",
                passed=True,
                message=f"Queried {len(available_catalogs)} Trino catalog(s).",
            )
        except Exception as exc:
            return ValidationCheck(
                name="catalog_query",
                passed=False,
                message=f"Could not query Trino catalogs: {exc}",
            )
        finally:
            if connection is not None:
                connection.close()  # type: ignore[no-untyped-call]


def _parse_trino_url(value: str) -> tuple[TrinoConnectionInfo | None, ValidationCheck]:
    raw_url = value if "://" in value else f"//{value}"
    try:
        try:
            parsed = urllib.parse.urlsplit(raw_url)
        except ValueError as exc:
            raise ValueError("URL could not be parsed") from exc
        try:
            explicit_port = parsed.port
        except ValueError as exc:
            raise ValueError("port must be an integer between 1 and 65535") from exc
        if explicit_port == 0:
            raise ValueError("port must be between 1 and 65535")
        scheme = parsed.scheme.lower() or ("https" if explicit_port == _DEFAULT_HTTPS_PORT else "http")
        if scheme not in {"http", "https"}:
            raise ValueError(f"unsupported scheme '{scheme}'")
        if not parsed.hostname:
            raise ValueError("hostname is missing")
        if parsed.username is not None or parsed.password is not None or parsed.path or "?" in value or "#" in value:
            raise ValueError("URL must contain only a scheme, hostname, and port")
        port = (
            explicit_port
            if explicit_port is not None
            else (_DEFAULT_HTTPS_PORT if scheme == "https" else _DEFAULT_HTTP_PORT)
        )
    except ValueError as exc:
        return None, ValidationCheck(
            name="trino_url",
            passed=False,
            message=f"Invalid trino_url: {exc}",
        )

    return (
        TrinoConnectionInfo(host=parsed.hostname, port=port, http_scheme=scheme),
        ValidationCheck(
            name="trino_url",
            passed=True,
            message=f"Parsed Trino endpoint {parsed.hostname}:{port} using {scheme}.",
        ),
    )


def _parse_catalogs(value: str) -> tuple[list[str] | None, ValidationCheck]:
    try:
        raw_catalogs = json.loads(value)
    except json.JSONDecodeError as exc:
        return None, ValidationCheck(
            name="trino_catalogs",
            passed=False,
            message=f"trino_catalogs is not valid JSON: {exc}",
        )

    if not isinstance(raw_catalogs, list):
        return None, ValidationCheck(
            name="trino_catalogs",
            passed=False,
            message="trino_catalogs must be a JSON list.",
        )

    names: list[str] = []
    for index, catalog in enumerate(raw_catalogs):
        if not isinstance(catalog, dict) or not isinstance(catalog.get("name"), str):
            return None, ValidationCheck(
                name="trino_catalogs",
                passed=False,
                message=f"Catalog at index {index} must contain a string name.",
            )
        name = catalog["name"]
        if not name.strip():
            return None, ValidationCheck(
                name="trino_catalogs",
                passed=False,
                message=f"Catalog at index {index} has an empty name.",
            )
        for field in ("connector", "description"):
            if field in catalog and not isinstance(catalog[field], str):
                return None, ValidationCheck(
                    name="trino_catalogs",
                    passed=False,
                    message=f"Catalog '{name}' field '{field}' must be a string.",
                )
        names.append(name)

    if len(names) != len(set(names)):
        return None, ValidationCheck(
            name="trino_catalogs",
            passed=False,
            message="trino_catalogs contains duplicate names.",
        )

    return names, ValidationCheck(
        name="trino_catalogs",
        passed=True,
        message=f"Validated {len(names)} advertised catalog(s).",
    )


def _connect(connection_info: TrinoConnectionInfo, credentials: dict[str, str]) -> trino.dbapi.Connection:
    username = credentials["username"]
    kwargs: dict[str, Any] = {
        "host": connection_info.host,
        "port": connection_info.port,
        "http_scheme": connection_info.http_scheme,
        "user": username,
        "request_timeout": _REQUEST_TIMEOUT_SECONDS,
    }
    # Trino's insecure HTTP path trusts the user header and bypasses password authentication;
    # sending Basic Authentication there would expose the password without validating it.
    if connection_info.http_scheme == "https":
        kwargs["auth"] = trino.auth.BasicAuthentication(username, credentials["password"])
    return trino.dbapi.connect(**kwargs)  # type: ignore[no-any-return,no-untyped-call]
