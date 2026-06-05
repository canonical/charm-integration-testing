# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import tempfile
import uuid
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

# Key used by s3-integrator to expose credentials as a Juju secret.
_SECRET_KEY = "s3-credentials"  # nosec B105

# Required fields that must be present in the relation databag (or resolved secret).
_REQUIRED_FIELDS = ("bucket", "access-key", "secret-key")

# S3 client timeouts and retry configuration.
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 10
_MAX_RETRY_ATTEMPTS = 1


class S3Validator(BaseValidator):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ca_file_path: str | None = None

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level == "uat":
            return self._skipped_result_due_to_level(level)
        error_result = self._check_relation_exists(level)
        if error_result:
            return error_result
        if level == "simple":
            return self._validate_simple()
        if level == "deep":
            return self._validate_deep()
        return self._skipped_result_due_to_level(level)

    # ------------------------------------------------------------------
    # Simple (L1): schema + bucket accessibility
    # ------------------------------------------------------------------

    def _validate_simple(self) -> ValidationResult:
        checks: list[ValidationCheck] = []

        creds = self._resolve_credentials()

        schema_check = self.validate_schema(list(_REQUIRED_FIELDS), creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("simple", checks)

        try:
            client = self._build_client(creds)
        except Exception as exc:
            checks.append(ValidationCheck(name="client_init", passed=False, message=str(exc)))
            return self._build_result("simple", checks)

        try:
            bucket = (self.databag | creds)["bucket"]
            head_check = self._head_bucket(client, bucket)
            checks.append(head_check)
        finally:
            self._cleanup_client(client)

        return self._build_result("simple", checks)

    # ------------------------------------------------------------------
    # Deep (L2): schema + bucket accessibility + write/read/delete canary
    # ------------------------------------------------------------------

    def _validate_deep(self) -> ValidationResult:
        checks: list[ValidationCheck] = []

        creds = self._resolve_credentials()

        schema_check = self.validate_schema(list(_REQUIRED_FIELDS), creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("deep", checks)

        try:
            client = self._build_client(creds)
        except Exception as exc:
            checks.append(ValidationCheck(name="client_init", passed=False, message=str(exc)))
            return self._build_result("deep", checks)

        try:
            bucket = (self.databag | creds)["bucket"]
            path_prefix = self.databag.get("path", "").strip("/")

            head_check = self._head_bucket(client, bucket)
            checks.append(head_check)
            if not head_check.passed:
                return self._build_result("deep", checks)

            canary_suffix = f"__canary_{uuid.uuid4().hex[:8]}"
            canary_key = f"{path_prefix}/{canary_suffix}" if path_prefix else canary_suffix
            canary_body = b"s3-validator-canary"

            # Write
            write_check = self._put_object(client, bucket, canary_key, canary_body)
            checks.append(write_check)

            # Always attempt canary cleanup to avoid leaking objects on partial server-side writes.
            try:
                if write_check.passed:
                    # Read back and verify only if write succeeded
                    read_check = self._get_and_verify_object(client, bucket, canary_key, canary_body)
                    checks.append(read_check)
            finally:
                delete_check = self._delete_object(client, bucket, canary_key)
                checks.append(delete_check)
        finally:
            self._cleanup_client(client)

        return self._build_result("deep", checks)

    # ------------------------------------------------------------------
    # S3 operations
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

    def _check_relation_exists(self, level: ValidationLevel) -> ValidationResult | None:
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        return None

    def _resolve_credentials(self) -> dict[str, str]:
        return self.resolve_secret(_SECRET_KEY, "access-key", "secret-key")

    def _build_client(self, creds: dict[str, str]) -> Any:
        merged = self.databag | creds
        endpoint_url = merged.get("endpoint") or None
        region = merged.get("region") or "us-east-1"
        addressing_style = merged.get("s3-uri-style", "path")

        client_kwargs: dict[str, Any] = {
            "aws_access_key_id": merged["access-key"],
            "aws_secret_access_key": merged["secret-key"],
            "endpoint_url": endpoint_url,
            "region_name": region,
            "config": Config(
                s3={"addressing_style": addressing_style},
                connect_timeout=_CONNECT_TIMEOUT,
                read_timeout=_READ_TIMEOUT,
                retries={"max_attempts": _MAX_RETRY_ATTEMPTS},
            ),
        }

        tls_ca = merged.get("tls-ca-chain")
        if tls_ca:
            self._write_ca_file(tls_ca)
            client_kwargs["verify"] = self._ca_file_path

        try:
            return boto3.client("s3", **client_kwargs)
        except Exception:
            self._remove_ca_file()
            raise

    def _cleanup_client(self, client: Any) -> None:
        try:
            if hasattr(client, "close"):
                client.close()
        finally:
            self._remove_ca_file()

    def _write_ca_file(self, ca_content: str) -> None:
        self._remove_ca_file()
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".pem") as f:
            f.write(ca_content)
            self._ca_file_path = f.name

    def _remove_ca_file(self) -> None:
        if self._ca_file_path:
            if os.path.exists(self._ca_file_path):
                os.remove(self._ca_file_path)
            self._ca_file_path = None

    def _build_result(self, level: ValidationLevel, checks: list[ValidationCheck]) -> ValidationResult:
        return self._make_result(
            "PASS" if all(c.passed for c in checks) else "FAIL",
            level,
            checks,
        )
