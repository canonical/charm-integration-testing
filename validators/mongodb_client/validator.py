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

import os
import tempfile
from typing import Any
from urllib.parse import quote_plus

from pymongo import MongoClient

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult


class MongoDBClientValidator(BaseValidator):
    interface = "mongodb_client"

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if level != "simple":
            return ValidationResult(
                status="ERROR",
                endpoint=self.endpoint,
                interface=self.interface,
                level=level,
                relation_id=self.relation_id,
                error=f"Level '{level}' is not yet implemented.",
            )

        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        if not self.relation_exists():
            return ValidationResult(
                status="ERROR",
                endpoint=self.endpoint,
                interface=self.interface,
                level="simple",
                relation_id=self.relation_id,
                error=f"No remote application on relation '{self.endpoint}'.",
            )

        # --- 2. Resolve credentials (plain fields or Juju secrets) ---

        creds = {
            **self.resolve_secret("secret-user", "username", "password"),
            **self.resolve_secret("secret-tls", "tls", "tls-ca"),
        }

        # --- 3. Schema check ---
        schema = ["endpoints", "database", "username", "password"]

        checks.append(self.validate_schema(schema, creds))
        if not all(c.passed for c in checks):
            return ValidationResult(
                status="FAIL",
                endpoint=self.endpoint,
                interface=self.interface,
                level="simple",
                relation_id=self.relation_id,
                checks=checks,
            )

        # --- 4. Connect ---
        endpoint = self.databag["endpoints"].split(",")[0].strip()
        mongodb_client: MongoClient[Any] | None = None
        client_kwargs: dict[str, Any] = {}
        ca_file_path: str | None = None
        try:
            # example taken from https://pymongo.readthedocs.io/en/stable/api/pymongo/mongo_client.html#pymongo.mongo_client.MongoClient
            uri = f"mongodb://{quote_plus(creds['username'])}:{quote_plus(creds['password'])}@{endpoint}"

            # Add TLS support if credentials are provided

            if creds.get("tls") and creds.get("tls-ca"):
                client_kwargs["tls"] = True
                with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".pem") as ca_file:
                    ca_file.write(creds["tls-ca"])
                ca_file_path = ca_file.name
                client_kwargs["tlsCAFile"] = ca_file_path

            mongodb_client = MongoClient(uri, **client_kwargs)
            mongodb_client.admin.command("ping")
            checks.append(ValidationCheck(name="connect", passed=True, message=f"Connected to {endpoint}."))
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return ValidationResult(
                status="FAIL",
                endpoint=self.endpoint,
                interface=self.interface,
                level="simple",
                relation_id=self.relation_id,
                checks=checks,
            )

        # --- 5. Canary query ---
        try:
            db = mongodb_client[self.databag["database"]]
            db.list_collections()
            checks.append(ValidationCheck(name="query", passed=True, message="Retrieved collection list successfully."))
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
        finally:
            if mongodb_client:
                mongodb_client.close()
            if ca_file_path:
                try:
                    os.remove(ca_file_path)
                except Exception:  # nosec B110
                    pass

        status = "PASS" if all(c.passed for c in checks) else "FAIL"
        return ValidationResult(
            status=status,
            endpoint=self.endpoint,
            interface=self.interface,
            level="simple",
            relation_id=self.relation_id,
            checks=checks,
        )
