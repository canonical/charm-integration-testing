# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import uuid
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

# Required fields that must be present in the relation databag.
_REQUIRED_FIELDS = ("endpoint", "bucket", "access-key", "secret-key")

# S3 client timeouts and retry configuration.
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 10
_MAX_RETRY_ATTEMPTS = 1


class ObjectStorageValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level == "uat":
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        if level == "simple":
            return self._validate_simple()
        if level == "deep":
            return self._validate_deep()
        return self._skipped_result_due_to_level(level)

    # ------------------------------------------------------------------
    # Simple (L1): schema + authenticate + bucket accessibility
    # ------------------------------------------------------------------

    def _validate_simple(self) -> ValidationResult:
        checks: list[ValidationCheck] = []

        schema_check = self.validate_schema(list(_REQUIRED_FIELDS))
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("simple", checks)

        try:
            client = self._build_client()
        except Exception as exc:
            checks.append(ValidationCheck(name="client_init", passed=False, message=str(exc)))
            return self._build_result("simple", checks)

        try:
            bucket_check = self._head_bucket(client, self.databag["bucket"])
            checks.append(bucket_check)
        finally:
            self._close_client(client)

        return self._build_result("simple", checks)

    # ------------------------------------------------------------------
    # Deep (L2): schema + bucket accessibility + write/read/delete canary
    # ------------------------------------------------------------------

    def _validate_deep(self) -> ValidationResult:
        checks: list[ValidationCheck] = []

        schema_check = self.validate_schema(list(_REQUIRED_FIELDS))
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("deep", checks)

        try:
            client = self._build_client()
        except Exception as exc:
            checks.append(ValidationCheck(name="client_init", passed=False, message=str(exc)))
            return self._build_result("deep", checks)

        try:
            bucket = self.databag["bucket"]

            bucket_check = self._head_bucket(client, bucket)
            checks.append(bucket_check)
            if not bucket_check.passed:
                return self._build_result("deep", checks)

            canary_key = f"__canary_{uuid.uuid4().hex[:8]}"
            canary_body = b"object-storage-validator-canary"

            write_check = self._put_object(client, bucket, canary_key, canary_body)
            checks.append(write_check)

            # Always attempt cleanup to avoid leaking canary objects on partial writes.
            try:
                if write_check.passed:
                    read_check = self._get_and_verify_object(client, bucket, canary_key, canary_body)
                    checks.append(read_check)
            finally:
                delete_check = self._delete_object(client, bucket, canary_key)
                checks.append(delete_check)
        finally:
            self._close_client(client)

        return self._build_result("deep", checks)

    # ------------------------------------------------------------------
    # S3-compatible operations
    # ------------------------------------------------------------------

    def _head_bucket(self, client: Any, bucket: str) -> ValidationCheck:
        try:
            client.head_bucket(Bucket=bucket)
            return ValidationCheck(name="bucket_accessible", passed=True, message=f"Bucket '{bucket}' is accessible.")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            return ValidationCheck(
                name="bucket_accessible",
                passed=False,
                message=f"Bucket '{bucket}' not accessible (error code {code}): {exc}",
            )
        except BotoCoreError as exc:
            return ValidationCheck(name="bucket_accessible", passed=False, message=str(exc))

    def _put_object(self, client: Any, bucket: str, key: str, body: bytes) -> ValidationCheck:
        try:
            client.put_object(Bucket=bucket, Key=key, Body=body)
            return ValidationCheck(name="write", passed=True, message=f"Wrote canary object '{key}'.")
        except (ClientError, BotoCoreError) as exc:
            return ValidationCheck(name="write", passed=False, message=str(exc))

    def _get_and_verify_object(self, client: Any, bucket: str, key: str, expected: bytes) -> ValidationCheck:
        try:
            response = client.get_object(Bucket=bucket, Key=key)
            try:
                body = response["Body"].read()
            finally:
                response["Body"].close()
            if body != expected:
                return ValidationCheck(
                    name="read_verify",
                    passed=False,
                    message=f"Canary body mismatch: expected {expected!r}, got {body!r}.",
                )
            return ValidationCheck(name="read_verify", passed=True, message="Canary object read and verified.")
        except (ClientError, BotoCoreError) as exc:
            return ValidationCheck(name="read_verify", passed=False, message=str(exc))

    def _delete_object(self, client: Any, bucket: str, key: str) -> ValidationCheck:
        try:
            client.delete_object(Bucket=bucket, Key=key)
            return ValidationCheck(name="cleanup", passed=True, message=f"Deleted canary object '{key}'.")
        except (ClientError, BotoCoreError) as exc:
            return ValidationCheck(name="cleanup", passed=False, message=str(exc))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_client(self) -> Any:
        databag = self.databag
        endpoint_url = databag["endpoint"]
        region = databag.get("region") or "us-east-1"

        return boto3.client(
            "s3",
            aws_access_key_id=databag["access-key"],
            aws_secret_access_key=databag["secret-key"],
            endpoint_url=endpoint_url,
            region_name=region,
            config=Config(
                s3={"addressing_style": "path"},
                connect_timeout=_CONNECT_TIMEOUT,
                read_timeout=_READ_TIMEOUT,
                retries={"max_attempts": _MAX_RETRY_ATTEMPTS},
            ),
        )

    def _close_client(self, client: Any) -> None:
        if hasattr(client, "close"):
            client.close()

    def _build_result(self, level: ValidationLevel, checks: list[ValidationCheck]) -> ValidationResult:
        return self._make_result(
            "PASS" if all(c.passed for c in checks) else "FAIL",
            level,
            checks,
        )
