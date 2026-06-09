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

import uuid
from typing import Any

import boto3  # type: ignore[import-untyped]
import yaml
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

# Fields required inside the SDI-encoded 'data' YAML provided by the minio charm.
_REQUIRED_SDI_FIELDS = ("service", "namespace", "port", "access-key", "secret-key")

# S3 client timeouts and retry configuration.
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 10
_MAX_RETRY_ATTEMPTS = 1


class ObjectStorageValidator(BaseValidator):
    """Validator for the object-storage interface (minio / SDI versioned protocol).

    The minio charm encodes its connection data as a YAML string inside the
    'data' key of the relation databag, following the Serialized Data Interface
    (SDI) convention.  Required fields within that YAML:
      service, namespace, port, access-key, secret-key
    """

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
    # Simple (L1): schema + create / verify / delete a transient bucket
    # ------------------------------------------------------------------

    def _validate_simple(self) -> ValidationResult:
        checks: list[ValidationCheck] = []

        sdi_data, schema_check = self._parse_and_check_schema()
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("simple", checks)

        try:
            client = self._build_client(sdi_data)
        except Exception as exc:
            checks.append(ValidationCheck(name="client_init", passed=False, message=str(exc)))
            return self._build_result("simple", checks)

        bucket = _test_bucket_name()
        bucket_created = False
        try:
            create_check = self._create_bucket(client, bucket)
            checks.append(create_check)
            bucket_created = create_check.passed
        finally:
            if bucket_created:
                checks.append(self._delete_bucket(client, bucket))
            self._close_client(client)

        return self._build_result("simple", checks)

    # ------------------------------------------------------------------
    # Deep (L2): schema + create bucket + write / read / delete canary
    # ------------------------------------------------------------------

    def _validate_deep(self) -> ValidationResult:
        checks: list[ValidationCheck] = []

        sdi_data, schema_check = self._parse_and_check_schema()
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("deep", checks)

        try:
            client = self._build_client(sdi_data)
        except Exception as exc:
            checks.append(ValidationCheck(name="client_init", passed=False, message=str(exc)))
            return self._build_result("deep", checks)

        bucket = _test_bucket_name()
        canary_key = f"__canary_{uuid.uuid4().hex[:8]}"
        canary_body = b"object-storage-validator-canary"
        bucket_created = False
        try:
            create_check = self._create_bucket(client, bucket)
            checks.append(create_check)
            bucket_created = create_check.passed
            if not bucket_created:
                return self._build_result("deep", checks)

            write_check = self._put_object(client, bucket, canary_key, canary_body)
            checks.append(write_check)

            # Always attempt canary cleanup to avoid leaking objects on partial failure.
            try:
                if write_check.passed:
                    checks.append(self._get_and_verify_object(client, bucket, canary_key, canary_body))
            finally:
                checks.append(self._delete_object(client, bucket, canary_key))
        finally:
            if bucket_created:
                checks.append(self._delete_bucket(client, bucket))
            self._close_client(client)

        return self._build_result("deep", checks)

    # ------------------------------------------------------------------
    # SDI parsing
    # ------------------------------------------------------------------

    def _parse_sdi_data(self) -> dict[str, str]:
        """Return the provider data decoded from the SDI 'data' YAML field."""
        data_str = self.databag.get("data", "")
        if not data_str:
            return {}
        parsed = yaml.safe_load(data_str)
        return parsed if isinstance(parsed, dict) else {}

    def _parse_and_check_schema(self) -> tuple[dict[str, str], ValidationCheck]:
        sdi_data = self._parse_sdi_data()
        missing = [f for f in _REQUIRED_SDI_FIELDS if not sdi_data.get(f)]
        check = ValidationCheck(
            name="schema",
            passed=not missing,
            message="OK" if not missing else f"Missing: {', '.join(missing)}",
        )
        return sdi_data, check

    # ------------------------------------------------------------------
    # S3-compatible operations
    # ------------------------------------------------------------------

    def _create_bucket(self, client: Any, bucket: str) -> ValidationCheck:
        try:
            client.create_bucket(Bucket=bucket)
            return ValidationCheck(name="bucket_create", passed=True, message=f"Created bucket '{bucket}'.")
        except (ClientError, BotoCoreError) as exc:
            return ValidationCheck(name="bucket_create", passed=False, message=str(exc))

    def _delete_bucket(self, client: Any, bucket: str) -> ValidationCheck:
        try:
            client.delete_bucket(Bucket=bucket)
            return ValidationCheck(name="bucket_cleanup", passed=True, message=f"Deleted bucket '{bucket}'.")
        except (ClientError, BotoCoreError) as exc:
            return ValidationCheck(name="bucket_cleanup", passed=False, message=str(exc))

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
            return ValidationCheck(name="canary_cleanup", passed=True, message=f"Deleted canary object '{key}'.")
        except (ClientError, BotoCoreError) as exc:
            return ValidationCheck(name="canary_cleanup", passed=False, message=str(exc))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_client(self, sdi_data: dict[str, str]) -> Any:
        scheme = "https" if str(sdi_data.get("secure", "false")).lower() == "true" else "http"
        endpoint_url = f"{scheme}://{sdi_data['service']}.{sdi_data['namespace']}.svc.cluster.local:{sdi_data['port']}"

        return boto3.client(
            "s3",
            aws_access_key_id=sdi_data["access-key"],
            aws_secret_access_key=sdi_data["secret-key"],
            endpoint_url=endpoint_url,
            region_name="us-east-1",
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


def _test_bucket_name() -> str:
    return f"validator-{uuid.uuid4().hex[:8]}"
