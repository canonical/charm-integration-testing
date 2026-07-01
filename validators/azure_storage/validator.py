# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import re

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

# Required fields in the provider application databag.
_REQUIRED_FIELDS = ("container", "storage-account", "secret-key")

# Juju secret key used by azure-storage-integrator to expose the storage secret key.
_SECRET_KEY = "secret-extra"  # nosec B105

# Valid values for the connection-protocol field.
_VALID_PROTOCOLS = frozenset(("wasb", "wasbs", "abfs", "abfss", "http", "https"))

# Regex for validating the endpoint URL format when present.
_ENDPOINT_RE = re.compile(rf"^({'|'.join(map(re.escape, sorted(_VALID_PROTOCOLS)))})://")


class AzureStorageValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple",):
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        return self._validate_simple()

    def _validate_simple(self) -> ValidationResult:
        """L1: Verify required fields are present and well-formed."""
        checks: list[ValidationCheck] = []

        creds = self._resolve_credentials()

        schema_check = self.validate_schema(list(_REQUIRED_FIELDS), creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        protocol_check = self._check_connection_protocol()
        checks.append(protocol_check)
        if not protocol_check.passed:
            return self._make_result(level="simple", checks=checks)

        endpoint_check = self._check_endpoint_format()
        if endpoint_check is not None:
            checks.append(endpoint_check)

        return self._make_result(level="simple", checks=checks)

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve secret-key from the databag or from a Juju secret."""
        return self.resolve_secret(_SECRET_KEY, "secret-key")

    def _check_connection_protocol(self) -> ValidationCheck:
        """Validate connection-protocol if present; accept any value in _VALID_PROTOCOLS."""
        protocol = self.databag.get("connection-protocol", "")
        if not protocol:
            return ValidationCheck(
                name="connection_protocol",
                passed=True,
                message="No connection-protocol set; default (abfss) will be used.",
            )
        if protocol in _VALID_PROTOCOLS:
            return ValidationCheck(
                name="connection_protocol",
                passed=True,
                message=f"connection-protocol '{protocol}' is valid.",
            )
        return ValidationCheck(
            name="connection_protocol",
            passed=False,
            message=(
                f"connection-protocol '{protocol}' is not recognised. "
                f"Expected one of: {', '.join(sorted(_VALID_PROTOCOLS))}."
            ),
        )

    def _check_endpoint_format(self) -> ValidationCheck | None:
        """Return a ValidationCheck for the endpoint URL format, or None if absent."""
        endpoint = self.databag.get("endpoint", "").strip()
        if not endpoint:
            return None
        if _ENDPOINT_RE.match(endpoint):
            return ValidationCheck(
                name="endpoint_format",
                passed=True,
                message=f"Endpoint URL format is valid: {endpoint}",
            )
        return ValidationCheck(
            name="endpoint_format",
            passed=False,
            message=(
                f"Endpoint URL '{endpoint}' does not begin with a recognised Azure "
                "Storage scheme (wasb, wasbs, abfs, abfss, http, https)."
            ),
        )
