# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Validator for the legacy ``mysql`` Juju interface.

The ``mysql`` interface is the historical MySQL relation still exposed by the
Canonical ``mysql`` charm (endpoint ``mysql``) for backwards compatibility with
older requirers such as ``apache-guacamole``, ``wordpress`` and ``mediawiki``.

Unlike the modern ``mysql_client`` interface, the legacy ``mysql`` interface is
**unit scoped**: the provider publishes the connection details on its *unit*
databag rather than the application databag, and credentials are plain fields
(no Juju secrets).

Connection fields published by the provider unit:

    host            MySQL server hostname or IP.
    port            MySQL port (string; defaults to ``3306`` when absent).
    user            Application-scoped MySQL user.
    password        Password for ``user``.
    database        Name of the granted database.
    root_password   Root password (not used by this validator).

Because the data is unit scoped, the databag that carries the connection
details depends on which side the validator runs:

    provides    The provider (e.g. ``mysql``) publishes the details on its own
                unit databag, so we read the *local* unit databag.
    requires    The requirer reads the provider's published details, which live
                on the *remote* provider unit databag(s).

Both roles perform the same live connection probe against the MySQL server.
"""

import time
import uuid

import pymysql

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

# Fields the provider must publish for a usable connection. ``port`` is handled
# separately because it has a well-defined protocol default.
_REQUIRED_FIELDS = ("host", "user", "password", "database")

_DEFAULT_PORT = 3306
_MIN_PORT = 1
_MAX_PORT = 65535

_CONNECT_TIMEOUT_SECS = 5
_DEEP_TIMEOUT_SECS = 10

_CANARY_MARKER = "validator-probe"


class MySQLValidator(BaseValidator):
    """Runtime validator for the legacy ``mysql`` interface."""

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role not in ("requires", "provides"):
            return self._skipped_result_due_to_role(level, self.role)

        if level == "simple":
            return self._validate_simple()
        if level == "deep":
            return self._validate_deep()
        return self._skipped_result_due_to_level(level)

    # ------------------------------------------------------------------
    # Validation levels
    # ------------------------------------------------------------------

    def _validate_simple(self) -> ValidationResult:
        """L1: connectivity and auth with a read-only ``SELECT 1`` probe."""
        checks: list[ValidationCheck] = []

        error = self._check_relation_exists("simple")
        if error:
            return error

        data = self._connection_data()

        schema_check = _check_schema(data)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        fields_check = _check_field_constraints(data)
        checks.append(fields_check)
        if not fields_check.passed:
            return self._make_result(level="simple", checks=checks)

        try:
            conn = self._connect(data)
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=_connect_error(data, exc)))
            return self._make_result(level="simple", checks=checks)

        checks.append(
            ValidationCheck(
                name="connect",
                passed=True,
                message=f"Connected to {data['host']}:{_resolve_port(data)}.",
            )
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
            if row and row[0] == 1:
                checks.append(ValidationCheck(name="query", passed=True, message="SELECT 1 OK."))
            else:
                checks.append(
                    ValidationCheck(name="query", passed=False, message=f"Unexpected SELECT 1 result: {row!r}.")
                )
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
        finally:
            conn.close()

        return self._make_result(level="simple", checks=checks)

    def _validate_deep(self) -> ValidationResult:
        """L2: read/write capability using a canary table (create/write/verify/drop)."""
        start_time = time.monotonic()
        checks: list[ValidationCheck] = []

        error = self._check_relation_exists("deep")
        if error:
            return error

        data = self._connection_data()

        schema_check = _check_schema(data)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        fields_check = _check_field_constraints(data)
        checks.append(fields_check)
        if not fields_check.passed:
            return self._make_result(level="deep", checks=checks)

        try:
            conn = self._connect(data)
            conn.autocommit(True)
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=_connect_error(data, exc)))
            return self._make_result(level="deep", checks=checks)

        checks.append(
            ValidationCheck(
                name="connect",
                passed=True,
                message=f"Connected to {data['host']}:{_resolve_port(data)}.",
            )
        )

        canary_table = f"__canary_{uuid.uuid4().hex[:8]}"
        try:
            checks.append(self._run_canary(conn, canary_table))
        finally:
            checks.append(self._drop_canary(conn, canary_table))
            conn.close()

        elapsed = time.monotonic() - start_time
        checks.append(
            ValidationCheck(
                name="latency",
                passed=elapsed <= _DEEP_TIMEOUT_SECS,
                message=(
                    f"Deep validation completed in {elapsed:.1f}s."
                    if elapsed <= _DEEP_TIMEOUT_SECS
                    else f"Deep validation took {elapsed:.1f}s, exceeded {_DEEP_TIMEOUT_SECS}s limit."
                ),
            )
        )

        return self._make_result(level="deep", checks=checks)

    # ------------------------------------------------------------------
    # Canary helpers
    # ------------------------------------------------------------------

    def _run_canary(self, conn: "pymysql.connections.Connection", table: str) -> ValidationCheck:
        """Create the canary table, write a row, and read it back for verification."""
        try:
            with conn.cursor() as cur:
                cur.execute(  # nosec B608 - table name is UUID-generated
                    f"CREATE TABLE {table} (id INT AUTO_INCREMENT PRIMARY KEY, marker VARCHAR(255) NOT NULL)"
                )
                cur.execute(
                    f"INSERT INTO {table} (marker) VALUES (%s)",  # nosec B608 - table name is UUID-generated
                    (_CANARY_MARKER,),
                )
                inserted_id = cur.lastrowid
                cur.execute(
                    f"SELECT marker FROM {table} WHERE id = %s",  # nosec B608 - table name is UUID-generated
                    (inserted_id,),
                )
                row = cur.fetchone()
        except Exception as exc:
            return ValidationCheck(name="write_read_verify", passed=False, message=str(exc))

        if row and row[0] == _CANARY_MARKER:
            return ValidationCheck(
                name="write_read_verify",
                passed=True,
                message="Successfully wrote, read, and verified canary row.",
            )
        return ValidationCheck(
            name="write_read_verify",
            passed=False,
            message=f"Read-back value {row!r} did not match written marker.",
        )

    def _drop_canary(self, conn: "pymysql.connections.Connection", table: str) -> ValidationCheck:
        """Drop the canary table; best effort but reported as its own check."""
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {table}")  # nosec B608 - table name is UUID-generated
        except Exception as exc:
            return ValidationCheck(name="cleanup", passed=False, message=f"Failed to drop canary table: {exc}")
        return ValidationCheck(name="cleanup", passed=True, message="Dropped canary table.")

    # ------------------------------------------------------------------
    # Data access helpers
    # ------------------------------------------------------------------

    def _check_relation_exists(self, level: ValidationLevel) -> ValidationResult | None:
        """Return an ERROR result if there is no remote application, else None."""
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        return None

    def _connection_data(self) -> dict[str, str]:
        """Return the connection databag for the current role.

        The legacy ``mysql`` interface is unit scoped, so the connection details
        published by the provider live on a *unit* databag. When validating the
        provider side we read our own unit databag; when validating the requirer
        side we merge the remote provider unit databag(s).
        """
        if self.role == "provides":
            return dict(self.relation.data.get(self.charm.unit, {}))

        merged: dict[str, str] = {}
        for unit in sorted(self.relation.units, key=lambda u: u.name):
            merged.update(self.relation.data.get(unit, {}))
        return merged

    def _connect(self, data: dict[str, str]) -> "pymysql.connections.Connection":
        """Open a PyMySQL connection using the resolved connection fields."""
        return pymysql.connect(
            host=data["host"],
            port=_resolve_port(data),
            user=data["user"],
            password=data["password"],
            database=data["database"],
            connect_timeout=_CONNECT_TIMEOUT_SECS,
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _resolve_port(data: dict[str, str]) -> int:
    """Return the connection port, falling back to the MySQL default 3306."""
    raw = str(data.get("port", "")).strip()
    if not raw:
        return _DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_PORT


def _check_schema(data: dict[str, str]) -> ValidationCheck:
    """Validate the required fields are present on the unit-scoped databag.

    Built directly from the unit-scoped connection ``data`` rather than
    ``BaseValidator.validate_schema()``: that helper overlays the *application*
    databag, which for this unit-scoped interface could let the schema check
    pass on unrelated app-level fields while the unit databag is incomplete.
    """
    missing = [f for f in _REQUIRED_FIELDS if not str(data.get(f, "")).strip()]
    return ValidationCheck(
        name="schema",
        passed=not missing,
        message="OK" if not missing else f"Missing: {', '.join(missing)}",
    )


def _check_field_constraints(data: dict[str, str]) -> ValidationCheck:
    """Validate value constraints on the connection fields.

    Ensures the host and credentials are non-empty and the port is a valid TCP
    port number. Remediation: verify the provider charm has completed the
    relation-created hook and published a well-formed databag.
    """
    problems: list[str] = []

    for field in ("host", "user", "password", "database"):
        if not str(data.get(field, "")).strip():
            problems.append(f"'{field}' is empty")

    raw_port = str(data.get("port", "")).strip()
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            problems.append(f"'port' value {raw_port!r} is not an integer")
        else:
            if not _MIN_PORT <= port <= _MAX_PORT:
                problems.append(f"'port' value {port} is out of range {_MIN_PORT}-{_MAX_PORT}")

    if problems:
        return ValidationCheck(
            name="field_constraints",
            passed=False,
            message="; ".join(problems) + ".",
        )
    return ValidationCheck(
        name="field_constraints",
        passed=True,
        message=f"Fields valid (host={data['host']}, port={_resolve_port(data)}, database={data['database']}).",
    )


def _connect_error(data: dict[str, str], exc: Exception) -> str:
    """Build an actionable connect-failure message."""
    return (
        f"Failed to connect to {data.get('host', '?')}:{_resolve_port(data)} "
        f"as user {data.get('user', '?')!r}: {exc}. "
        "Remediation: verify the MySQL server is running and the published credentials are valid."
    )
