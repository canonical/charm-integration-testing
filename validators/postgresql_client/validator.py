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

import psycopg2

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult


class PostgreSQLClientValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if level != "simple":
            return self._skipped_result(level)

        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        if self.relation.app is None:
            return self._make_result(
                status="ERROR",
                level="simple",
                error=f"No remote application on relation '{self.endpoint}'.",
            )

        databag = dict(self.relation.data[self.relation.app])

        # --- 2. Resolve credentials (plain fields or Juju secrets) ---
        def resolve_secret(uri_key: str, *fields: str) -> dict[str, str]:
            if uri := databag.get(uri_key):
                return self.charm.model.get_secret(id=uri).get_content()
            return {f: databag[f] for f in fields if f in databag}

        creds = {
            **resolve_secret("secret-user", "username", "password"),
            **resolve_secret("secret-tls", "tls", "tls-ca"),
        }

        # --- 3. Schema check ---
        missing = [f for f in ("endpoints", "database", "username", "password") if not (databag | creds).get(f)]
        checks.append(
            ValidationCheck(
                name="schema",
                passed=not missing,
                message="OK" if not missing else f"Missing: {', '.join(missing)}",
            )
        )
        if missing:
            return self._make_result(
                status="FAIL",
                level="simple",
                checks=checks,
            )

        # --- 4. Connect ---
        endpoint = databag["endpoints"].split(",")[0].strip()
        host, _, port_str = endpoint.rpartition(":")
        try:
            conn = psycopg2.connect(
                host=host or endpoint,
                port=int(port_str) if port_str.isdigit() else 5432,
                dbname=databag["database"],
                user=creds["username"],
                password=creds["password"],
                connect_timeout=5,
            )
            checks.append(ValidationCheck(name="connect", passed=True, message=f"Connected to {endpoint}."))
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(
                status="FAIL",
                level="simple",
                checks=checks,
            )

        # --- 5. Canary query ---
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                checks.append(ValidationCheck(name="query", passed=True, message="SELECT 1 OK."))
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
        finally:
            conn.close()

        status = "PASS" if all(c.passed for c in checks) else "FAIL"
        return self._make_result(
            status=status,
            level="simple",
            checks=checks,
        )
