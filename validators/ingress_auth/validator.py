# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

_REQUIRED_UNIT_FIELDS = ("ingress-address", "private-address", "egress-subnets")


class IngressAuthValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level != "simple":
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        # Resolve optional credentials if this provider chooses secret-backed fields.
        self.resolve_secret("secret-auth")

        required_app_fields = ["_supported_versions"] if "_supported_versions" in self.databag else []
        checks: list[ValidationCheck] = [self.validate_schema(required_app_fields)]
        checks.append(self._validate_remote_unit_databags())
        return self._make_result(level=level, checks=checks)

    def _validate_remote_unit_databags(self) -> ValidationCheck:
        units = sorted(self.relation.units, key=lambda unit: unit.name)
        if not units:
            return ValidationCheck(
                name="unit_databag",
                passed=False,
                message="No remote units are present on the relation.",
            )

        errors: list[str] = []
        for unit in units:
            unit_data = dict(self.relation.data[unit])
            missing = [field for field in _REQUIRED_UNIT_FIELDS if not unit_data.get(field)]
            if missing:
                errors.append(f"{unit.name}: Missing {', '.join(missing)}")

        if errors:
            return ValidationCheck(name="unit_databag", passed=False, message="; ".join(errors))
        return ValidationCheck(
            name="unit_databag",
            passed=True,
            message=f"Validated {len(units)} remote unit databag(s).",
        )
