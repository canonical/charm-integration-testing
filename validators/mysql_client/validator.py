# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import time
import uuid

import pymysql

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)


class MySQLClientValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)

        if level == "uat":
            return self._skipped_result_due_to_level(level)

        if level == "simple":
            return self._validate_simple()
        elif level == "deep":
            return self._validate_deep()
        else:
            return self._skipped_result_due_to_level(level)

    def _validate_simple(self) -> ValidationResult:
        """L1: Connectivity & auth with read-only canary query."""
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        error_result = self._check_relation_exists("simple")
        if error_result:
            return error_result

        # --- 2. Resolve credentials (plain fields or Juju secrets) ---
        creds = self._resolve_credentials()

        # --- 3. Schema check ---
        schema = ["endpoints", "database", "username", "password"]
        schema_check = self.validate_schema(schema, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        # --- 4. Connect ---
        data = self.databag | creds
        try:
            conn = self._connect(data)
            host, port = self._first_endpoint(data)
            checks.append(ValidationCheck(name="connect", passed=True, message=f"Connected to {host}:{port}."))
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="simple", checks=checks)

        # --- 5. Canary read-only query ---
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                checks.append(ValidationCheck(name="query", passed=True, message="SELECT 1 OK."))
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
        else:
            # --- 6. Optional server version check (only when query succeeded) ---
            try:
                with conn.cursor() as cur:
                    version_check = self._check_server_version(cur, data)
                    if version_check is not None:
                        checks.append(version_check)
            except Exception as exc:
                checks.append(ValidationCheck(name="version_consistency", passed=False, message=str(exc)))
        finally:
            conn.close()

        return self._make_result(level="simple", checks=checks)

    def _validate_deep(self) -> ValidationResult:
        """L2: Read/write capability with canary table (create, write, read-verify, cleanup)."""
        start_time = time.monotonic()
        timeout_secs = 10
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        error_result = self._check_relation_exists("deep")
        if error_result:
            return error_result

        # --- 2. Resolve credentials (plain fields or Juju secrets) ---
        creds = self._resolve_credentials()

        # --- 3. Schema check ---
        schema = ["endpoints", "database", "username", "password"]
        schema_check = self.validate_schema(schema, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        # --- 4. Connect ---
        data = self.databag | creds
        try:
            conn = self._connect(data)
            conn.autocommit(True)
            host, port = self._first_endpoint(data)
            checks.append(ValidationCheck(name="connect", passed=True, message=f"Connected to {host}:{port}."))
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="deep", checks=checks)

        # --- 5. Canary read-only query ---
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                checks.append(ValidationCheck(name="query", passed=True, message="SELECT 1 OK."))
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
            conn.close()
            return self._make_result(level="deep", checks=checks)

        # --- 6. Create canary table, write, read-verify, cleanup ---
        canary_table = f"__canary_{uuid.uuid4().hex[:8]}"
        try:
            with conn.cursor() as cur:
                # Create
                cur.execute(  # nosec B608 - table name is UUID-generated
                    f"CREATE TABLE {canary_table} (id INT AUTO_INCREMENT PRIMARY KEY, marker VARCHAR(255) NOT NULL)"
                )

                # Write
                insert_query = f"INSERT INTO {canary_table} (marker) VALUES (%s)"  # nosec B608 - table name is UUID-generated
                cur.execute(insert_query, ("validator-probe",))
                inserted_id = cur.lastrowid

                # Read-verify
                select_query = f"SELECT marker FROM {canary_table} WHERE id = %s"  # nosec B608 - table name is UUID-generated
                cur.execute(select_query, (inserted_id,))
                read_row = cur.fetchone()
                if read_row and read_row[0] == "validator-probe":
                    checks.append(
                        ValidationCheck(
                            name="write_read_verify",
                            passed=True,
                            message="Successfully wrote, read, and verified test row.",
                        )
                    )
                else:
                    checks.append(
                        ValidationCheck(
                            name="write_read_verify",
                            passed=False,
                            message="Failed to verify written row.",
                        )
                    )
        except Exception as exc:
            checks.append(
                ValidationCheck(
                    name="write_read_verify",
                    passed=False,
                    message=str(exc),
                )
            )

        # --- 7. Cleanup: drop canary table ---
        cleanup_passed = False
        cleanup_message = ""
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {canary_table}")  # nosec B608 - table name is UUID-generated
            cleanup_passed = True
            cleanup_message = "Dropped canary table."
        except Exception as exc:  # nosec B110 - best-effort cleanup
            cleanup_message = f"Failed to drop canary table: {exc}"
        finally:
            conn.close()

        checks.append(ValidationCheck(name="cleanup", passed=cleanup_passed, message=cleanup_message))

        # --- 8. Latency check ---
        elapsed = time.monotonic() - start_time
        if elapsed > timeout_secs:
            checks.append(
                ValidationCheck(
                    name="latency",
                    passed=False,
                    message=f"Deep validation took {elapsed:.1f}s, exceeded {timeout_secs}s limit.",
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    name="latency",
                    passed=True,
                    message=f"Deep validation completed in {elapsed:.1f}s.",
                )
            )

        return self._make_result(level="deep", checks=checks)

    def _check_relation_exists(self, level: ValidationLevel) -> ValidationResult | None:
        """Return an ERROR result if the remote app is absent, else None."""
        if not self.relation_exists():
            return self._make_result(
                status="ERROR",
                level=level,
                error=f"No remote application on relation '{self.endpoint}'.",
            )
        return None

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve credentials from the relation databag or Juju secrets."""
        return {
            **self.resolve_secret("secret-user", "username", "password"),
            **self.resolve_secret("secret-tls", "tls-ca"),
        }

    def _first_endpoint(self, data: dict[str, str]) -> tuple[str, int]:
        """Split the first `endpoints` entry into (host, port)."""
        first = data["endpoints"].split(",")[0].strip()
        host, _, port = first.partition(":")
        return host, int(port) if port else 3306

    def _connect(self, data: dict[str, str]) -> "pymysql.connections.Connection":
        """Open a PyMySQL connection using databag/secret fields."""
        host, port = self._first_endpoint(data)
        return pymysql.connect(
            host=host,
            port=port,
            user=data["username"],
            password=data["password"],
            database=data["database"],
            connect_timeout=5,
        )

    def _check_server_version(self, cur: "pymysql.cursors.Cursor", data: dict[str, str]) -> ValidationCheck | None:
        """Verify the databag `version` field matches the server-reported version. None when absent."""
        expected_version = data.get("version", "").strip()
        if not expected_version:
            return None
        cur.execute("SELECT VERSION()")
        row = cur.fetchone()
        actual_version = str(row[0]) if row else ""
        if actual_version.startswith(expected_version):
            return ValidationCheck(
                name="version_consistency",
                passed=True,
                message=f"Server version '{actual_version}' matches databag 'version' field.",
            )
        return ValidationCheck(
            name="version_consistency",
            passed=False,
            message=f"Server version '{actual_version}' does not match databag 'version' field '{expected_version}'.",
        )
