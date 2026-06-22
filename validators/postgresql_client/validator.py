# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import time
import urllib.parse
import uuid

import psycopg2

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)


class PostgreSQLClientValidator(BaseValidator):
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
        """L1: Connectivity & Auth with read-only canary query."""
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        error_result = self._check_relation_exists("simple")
        if error_result:
            return error_result

        # --- 2. Resolve credentials (plain fields or Juju secrets) ---
        creds = self._resolve_credentials()

        # --- 3. Schema check ---
        schema_check = self.validate_schema(["uris", "database", "username", "password"], creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        # --- 4. Database consistency check ---
        data = self.databag | creds
        uri = data["uris"].split(",")[0].strip()
        db_check = self._check_database_consistency(uri, data["database"])
        checks.append(db_check)
        if not db_check.passed:
            return self._make_result(level="simple", checks=checks)

        # --- 5. Connect ---
        try:
            conn = self._connect(uri)
            safe_target = self._safe_uri_string(uri)
            checks.append(ValidationCheck(name="connect", passed=True, message=f"Connected to {safe_target}."))
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="simple", checks=checks)

        # --- 6. Canary read-only query ---
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                checks.append(ValidationCheck(name="query", passed=True, message="SELECT 1 OK."))
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
        else:
            # --- 7. Optional extensions check (only when query succeeded) ---
            try:
                with conn.cursor() as cur:
                    ext_check = self._check_extensions(cur)
                    if ext_check is not None:
                        checks.append(ext_check)
            except Exception as exc:
                checks.append(ValidationCheck(name="extensions", passed=False, message=str(exc)))
        finally:
            conn.close()

        return self._make_result(level="simple", checks=checks)

    def _validate_deep(self) -> ValidationResult:
        """L2: Read/Write capability with canary table (create, write, read-verify, cleanup)."""
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
        schema_check = self.validate_schema(["uris", "database", "username", "password"], creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        # --- 4. Database consistency check ---
        data = self.databag | creds
        uri = data["uris"].split(",")[0].strip()
        db_check = self._check_database_consistency(uri, data["database"])
        checks.append(db_check)
        if not db_check.passed:
            return self._make_result(level="deep", checks=checks)

        # --- 5. Connect ---
        try:
            conn = self._connect(uri)
            conn.autocommit = True
            safe_target = self._safe_uri_string(uri)
            checks.append(ValidationCheck(name="connect", passed=True, message=f"Connected to {safe_target}."))
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="deep", checks=checks)

        # --- 6. Canary read-only query ---
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                checks.append(ValidationCheck(name="query", passed=True, message="SELECT 1 OK."))
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
            conn.close()
            return self._make_result(level="deep", checks=checks)

        # --- 7. Optional extensions check ---
        try:
            with conn.cursor() as cur:
                ext_check = self._check_extensions(cur)
                if ext_check is not None:
                    checks.append(ext_check)
                    if not ext_check.passed:
                        conn.close()
                        return self._make_result(level="deep", checks=checks)
        except Exception as exc:
            checks.append(ValidationCheck(name="extensions", passed=False, message=str(exc)))
            conn.close()
            return self._make_result(level="deep", checks=checks)

        # --- 8. Create canary table, write, read-verify, cleanup ---
        canary_table = f"__canary_{uuid.uuid4().hex[:8]}"
        try:
            with conn.cursor() as cur:
                # Create
                cur.execute(f"CREATE TABLE {canary_table} (id SERIAL PRIMARY KEY, marker TEXT NOT NULL)")  # nosec B608 - table name is UUID-generated

                # Write
                cur.execute(f"INSERT INTO {canary_table} (marker) VALUES (%s) RETURNING id", ("validator-probe",))  # nosec B608 - table name is UUID-generated
                row = cur.fetchone()
                inserted_id = row[0] if row else None

                # Read-verify
                if inserted_id is not None:
                    cur.execute(f"SELECT marker FROM {canary_table} WHERE id = %s", (inserted_id,))  # nosec B608 - table name is UUID-generated
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
                else:
                    checks.append(
                        ValidationCheck(
                            name="write_read_verify",
                            passed=False,
                            message="INSERT returned no ID.",
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

        # --- 9. Cleanup: drop canary table ---
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

        # --- 10. Latency check ---
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

    def _check_database_consistency(self, uri: str, expected_db: str) -> ValidationCheck:
        """Verify the database in the URI matches the `database` field in the databag."""
        try:
            parsed = urllib.parse.urlparse(uri)
            uri_db = urllib.parse.unquote(parsed.path.lstrip("/"))
        except Exception as exc:
            return ValidationCheck(name="database_consistency", passed=False, message=f"Could not parse URI: {exc}")
        if uri_db == expected_db:
            return ValidationCheck(
                name="database_consistency",
                passed=True,
                message=f"URI database '{uri_db}' matches databag 'database' field.",
            )
        return ValidationCheck(
            name="database_consistency",
            passed=False,
            message=f"URI database '{uri_db}' does not match databag 'database' field '{expected_db}'.",
        )

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
            **self.resolve_secret("secret-user", "username", "password", "uris"),
            **self.resolve_secret("secret-tls", "tls", "tls-ca"),
        }

    def _check_extensions(self, cur: "psycopg2.extensions.cursor") -> ValidationCheck | None:
        """Verify declared extensions are installed. Returns None when field is absent."""
        extensions_raw = self.databag.get("extensions", "").strip()
        if not extensions_raw:
            return None
        exts = [e.strip() for e in extensions_raw.split(",") if e.strip()]
        missing: list[str] = []
        for ext in exts:
            cur.execute("SELECT COUNT(*) FROM pg_extension WHERE extname = %s", (ext,))
            row = cur.fetchone()
            if not row or row[0] == 0:
                missing.append(ext)
        return ValidationCheck(
            name="extensions",
            passed=not missing,
            message="OK" if not missing else f"Missing: {', '.join(missing)}",
        )

    def _connect(self, uri: str) -> "psycopg2.extensions.connection":
        """Open a psycopg2 connection using a PostgreSQL URI."""
        return psycopg2.connect(dsn=uri, connect_timeout=5)

    def _safe_uri_string(self, uri: str) -> str:
        parsed = urllib.parse.urlsplit(uri)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        safe_target = f"{host}{port}{parsed.path or ''}"
        return safe_target
