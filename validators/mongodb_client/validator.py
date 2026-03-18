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
import time
import uuid
from typing import Any
from urllib.parse import quote_plus

from pymongo import MongoClient

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult


class MongoDBClientValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if level == "uat":
            return self._skipped_result(level)

        if level == "simple":
            return self._validate_simple()
        elif level == "deep":
            return self._validate_deep()
        else:
            return self._skipped_result(level)

    def _validate_simple(self) -> ValidationResult:
        """L1: Connectivity & Auth with read-only canary query."""
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

        # --- 4. Connect & ping ---
        endpoint = self.databag["endpoints"].split(",")[0].strip()
        mongodb_client: MongoClient[Any] | None = None
        client_kwargs: dict[str, Any] = {}
        ca_file_path: str | None = None
        try:
            uri = f"mongodb://{quote_plus(creds['username'])}:{quote_plus(creds['password'])}@{endpoint}"
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

        # --- 5. Canary read-only query ---
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

    def _validate_deep(self) -> ValidationResult:
        """L2: Read/Write Capability with canary collection (create, write, read-verify, cleanup)."""
        start_time = time.time()
        timeout_secs = 10
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        if not self.relation_exists():
            return ValidationResult(
                status="ERROR",
                endpoint=self.endpoint,
                interface=self.interface,
                level="deep",
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
                level="deep",
                relation_id=self.relation_id,
                checks=checks,
            )

        # --- 4. Connect ---
        endpoint = self.databag["endpoints"].split(",")[0].strip()
        mongodb_client: MongoClient[Any] | None = None
        client_kwargs: dict[str, Any] = {}
        ca_file_path: str | None = None
        try:
            uri = f"mongodb://{quote_plus(creds['username'])}:{quote_plus(creds['password'])}@{endpoint}"
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
                level="deep",
                relation_id=self.relation_id,
                checks=checks,
            )

        # --- 5. Create canary collection, write, read-verify, cleanup ---
        canary_collection = f"__canary_{uuid.uuid4().hex[:8]}"
        try:
            db = mongodb_client[self.databag["database"]]
            col = db[canary_collection]

            # Write a test document
            test_doc = {"_test": True, "timestamp": time.time()}
            result = col.insert_one(test_doc)
            inserted_id = result.inserted_id

            # Read back and verify
            read_doc = col.find_one({"_id": inserted_id})
            if read_doc is None or not read_doc.get("_test"):
                checks.append(
                    ValidationCheck(
                        name="write_read_verify",
                        passed=False,
                        message="Failed to verify written document.",
                    )
                )
            else:
                checks.append(
                    ValidationCheck(
                        name="write_read_verify",
                        passed=True,
                        message="Successfully wrote, read, and verified test document.",
                    )
                )

            # Cleanup: drop canary collection
            db.drop_collection(canary_collection)
            checks.append(ValidationCheck(name="cleanup", passed=True, message="Dropped canary collection."))

        except Exception as exc:
            checks.append(ValidationCheck(name="write_read_verify", passed=False, message=str(exc)))
            # Attempt cleanup on error
            try:
                if mongodb_client:
                    db = mongodb_client[self.databag["database"]]
                    db.drop_collection(canary_collection)
            except Exception:  # nosec B110
                pass
        finally:
            if mongodb_client:
                mongodb_client.close()
            if ca_file_path:
                try:
                    os.remove(ca_file_path)
                except Exception:  # nosec B110
                    pass

        elapsed = time.time() - start_time
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

        status = "PASS" if all(c.passed for c in checks) else "FAIL"
        return ValidationResult(
            status=status,
            endpoint=self.endpoint,
            interface=self.interface,
            level="deep",
            relation_id=self.relation_id,
            checks=checks,
        )
