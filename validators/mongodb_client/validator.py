# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import tempfile
import time
import uuid
from typing import Any
from urllib.parse import quote_plus

from pymongo import MongoClient

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult


class MongoDBClientValidator(BaseValidator):
    ca_file_path = None

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
            return self._build_result("simple", checks)

        # --- 4. Connect & ping ---
        endpoint = self.databag["endpoints"].split(",")[0].strip()
        try:
            mongodb_client = self._build_mongodb_client(creds)
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._build_result("simple", checks)

        try:
            connect_check = self._attempt_connection(mongodb_client, endpoint)
            checks.append(connect_check)
            if not connect_check.passed:
                return self._build_result("simple", checks)

            # --- 5. Canary read-only query ---
            assert mongodb_client is not None
            try:
                db = mongodb_client[self.databag["database"]]
                db.list_collections()
                checks.append(
                    ValidationCheck(name="query", passed=True, message="Retrieved collection list successfully.")
                )
            except Exception as exc:
                checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
        finally:
            self._cleanup_client(mongodb_client)

        return self._build_result("simple", checks)

    def _validate_deep(self) -> ValidationResult:
        """L2: Read/Write Capability with canary collection (create, write, read-verify, cleanup)."""
        start_time = time.time()
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
            return self._build_result("deep", checks)

        # --- 4. Connect ---
        endpoint = self.databag["endpoints"].split(",")[0].strip()
        try:
            mongodb_client = self._build_mongodb_client(creds)
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._build_result("deep", checks)

        try:
            connect_check = self._attempt_connection(mongodb_client, endpoint)
            checks.append(connect_check)
            if not connect_check.passed:
                return self._build_result("deep", checks)

            # --- 5. Create canary collection, write, read-verify, cleanup ---
            assert mongodb_client is not None
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

            except Exception as exc:
                checks.append(
                    ValidationCheck(
                        name="write_read_verify",
                        passed=False,
                        message=str(exc),
                    )
                )

            # --- 6. Cleanup: drop canary collection ---
            cleanup_passed = False
            cleanup_message = ""
            try:
                if mongodb_client is not None:
                    db = mongodb_client[self.databag["database"]]
                    db.drop_collection(canary_collection)
                    cleanup_passed = True
                    cleanup_message = "Dropped canary collection."
            except Exception as exc:  # nosec B110 - best-effort cleanup
                cleanup_message = f"Failed to drop canary collection: {exc}"

            checks.append(
                ValidationCheck(
                    name="cleanup",
                    passed=cleanup_passed,
                    message=cleanup_message,
                )
            )
        finally:
            self._cleanup_client(mongodb_client)

        # --- 7. Latency check ---
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

        return self._build_result("deep", checks)

    def _check_relation_exists(self, level: str) -> ValidationResult | None:
        """Check if remote app exists on relation. Returns error result if missing, else None."""
        if not self.relation_exists():
            return ValidationResult(
                status="ERROR",
                endpoint=self.endpoint,
                interface=self.interface,
                level=level,
                relation_id=self.relation_id,
                error=f"No remote application on relation '{self.endpoint}'.",
            )
        return None

    def _resolve_credentials(self) -> dict[str, Any]:
        """Resolve credentials from relation data/secrets."""
        return {
            **self.resolve_secret("secret-user", "username", "password"),
            **self.resolve_secret("secret-tls", "tls", "tls-ca"),
        }

    def _validate_schema_or_return(self, level: str, creds: dict[str, str]) -> ValidationResult | None:
        """Validate schema. Returns error result if invalid, else None."""
        # Deprecated: inline schema validation instead (added to checks directly in methods).
        # Kept for backward compatibility.
        schema = ["endpoints", "database", "username", "password"]
        checks = [self.validate_schema(schema, creds)]
        if not all(c.passed for c in checks):
            return ValidationResult(
                status="FAIL",
                endpoint=self.endpoint,
                interface=self.interface,
                level=level,
                relation_id=self.relation_id,
                checks=checks,
            )
        return None

    def _build_mongodb_client(self, creds: dict[str, Any]) -> MongoClient[Any]:
        """Build and return MongoDB client with TLS/timeout config."""
        endpoint = self.databag["endpoints"].split(",")[0].strip()
        client_kwargs: dict[str, Any] = {
            "serverSelectionTimeoutMS": 5000,
            "connectTimeoutMS": 5000,
            "socketTimeoutMS": 10000,
            "appname": "mongodb-client-validator",
        }

        uri = f"mongodb://{quote_plus(creds['username'])}:{quote_plus(creds['password'])}@{endpoint}"
        if creds.get("tls") and creds.get("tls-ca"):
            client_kwargs["tls"] = True
            self._create_temp_ca_file(creds["tls-ca"])
            client_kwargs["tlsCAFile"] = self.ca_file_path

        return MongoClient(uri, **client_kwargs)

    def _attempt_connection(self, mongodb_client: MongoClient[Any] | None, endpoint: str) -> ValidationCheck:
        """Attempt to connect and ping the MongoDB server."""
        try:
            if mongodb_client is None:
                raise ValueError("MongoDB client is None")
            mongodb_client.admin.command("ping")
            return ValidationCheck(name="connect", passed=True, message=f"Connected to {endpoint}.")
        except Exception as exc:
            return ValidationCheck(name="connect", passed=False, message=str(exc))

    def _build_result(self, level: str, checks: list[ValidationCheck]) -> ValidationResult:
        """Build a ValidationResult from checks list."""
        status = "PASS" if all(c.passed for c in checks) else "FAIL"
        return ValidationResult(
            status=status,
            endpoint=self.endpoint,
            interface=self.interface,
            level=level,
            relation_id=self.relation_id,
            checks=checks,
        )

    def _cleanup_client(self, mongodb_client: MongoClient[Any] | None) -> None:
        """Clean up MongoDB client and temporary CA file."""
        if mongodb_client is not None:
            mongodb_client.close()

        self._remove_temp_ca_file()

    def _create_temp_ca_file(self, ca_content: str) -> None:
        """Create a temporary file with the given CA content and store path in self.ca_file_path."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".pem") as ca_file:
            ca_file.write(ca_content)
            self.ca_file_path = ca_file.name

    def _remove_temp_ca_file(self) -> None:
        """Remove the temporary CA file."""
        if self.ca_file_path and os.path.exists(self.ca_file_path):
            os.remove(self.ca_file_path)
            self.ca_file_path = None
