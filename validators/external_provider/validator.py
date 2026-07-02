# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

# Providers that use asymmetric keys instead of a shared client_secret.
_KEYAUTH_PROVIDERS = frozenset({"apple"})

# Allowed provider names (sourced from the interface library ALLOWED_PROVIDERS).
_ALLOWED_PROVIDERS = frozenset(
    {
        "generic",
        "google",
        "facebook",
        "microsoft",
        "github",
        "apple",
        "gitlab",
        "auth0",
        "slack",
        "spotify",
        "discord",
        "twitch",
        "netid",
        "yander",
        "vk",
        "dingtalk",
    }
)


class ExternalProviderValidator(BaseValidator):
    """Validator for the external_provider interface (Kratos external IdP)."""

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level != "simple":
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        return self._validate_simple()

    def _validate_simple(self) -> ValidationResult:
        checks: list[ValidationCheck] = []

        # --- 1. Schema: 'providers' key must be present ---
        schema_check = self.validate_schema(["providers"])
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        # --- 2. Parse the JSON-encoded providers list ---
        providers_raw = self.databag["providers"]
        parse_check, providers = self._parse_providers(providers_raw)
        checks.append(parse_check)
        if not parse_check.passed:
            return self._make_result(level="simple", checks=checks)

        # --- 3. At least one provider must be configured ---
        non_empty_check = ValidationCheck(
            name="providers_non_empty",
            passed=len(providers) > 0,
            message="OK" if providers else "No providers configured in databag.",
        )
        checks.append(non_empty_check)
        if not non_empty_check.passed:
            return self._make_result(level="simple", checks=checks)

        # --- 4. Each provider entry must contain required fields ---
        fields_check = self._check_provider_fields(providers)
        checks.append(fields_check)

        return self._make_result(level="simple", checks=checks)

    def _parse_providers(self, raw: str) -> tuple[ValidationCheck, list[dict[str, str]]]:
        """Attempt to JSON-decode the providers value."""
        try:
            providers = json.loads(raw)
            if not isinstance(providers, list):
                return ValidationCheck(
                    name="providers_json",
                    passed=False,
                    message=f"Expected a JSON array for 'providers', got {type(providers).__name__}.",
                ), []
            return ValidationCheck(name="providers_json", passed=True, message="OK"), providers
        except json.JSONDecodeError as exc:
            return ValidationCheck(
                name="providers_json",
                passed=False,
                message=f"Failed to decode 'providers' JSON: {exc}",
            ), []

    def _check_provider_fields(self, providers: list[dict[str, str]]) -> ValidationCheck:
        """Verify each provider entry contains the required fields."""
        issues: list[str] = []
        for idx, entry in enumerate(providers):
            if not isinstance(entry, dict):
                issues.append(f"providers[{idx}] is not an object (got {type(entry).__name__})")
                continue

            # provider and client_id are required for all types
            for required in ("provider", "client_id"):
                if not entry.get(required):
                    issues.append(f"providers[{idx}] missing: {required}")

            # Validate provider name is known
            provider_name = entry.get("provider", "")
            if provider_name and provider_name not in _ALLOWED_PROVIDERS:
                issues.append(f"providers[{idx}] unknown provider type: '{provider_name}'")

            # client_secret is required for all providers except those using asymmetric keys
            if provider_name not in _KEYAUTH_PROVIDERS and not entry.get("client_secret"):
                issues.append(f"providers[{idx}] missing: client_secret")

        return ValidationCheck(
            name="provider_fields",
            passed=not issues,
            message="OK" if not issues else "; ".join(issues),
        )
