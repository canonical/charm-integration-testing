# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import base64
import json
import lzma
from typing import Any

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

_REQUIRED_TEMPLATE_FIELDS = ("content", "charm", "juju_topology", "inject_dropdowns")


class GrafanaDashboardValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        checks: list[ValidationCheck] = []

        schema_check, outer = _parse_dashboards(self.databag)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._fail_result(level, checks)

        units = self.relation.units
        units_check = ValidationCheck(
            name="units_present",
            passed=bool(units),
            message="OK" if units else "Provider has no active units",
        )
        checks.append(units_check)
        if not units_check.passed:
            return self._fail_result(level, checks)

        structure_check = _validate_structure(outer)
        checks.append(structure_check)
        if not structure_check.passed:
            return self._fail_result(level, checks)

        if level == "deep":
            content_check = _validate_content(outer["templates"])
            checks.append(content_check)

        return self._make_result(level=level, checks=checks)


# ---------------------------------------------------------------------------
# Pure helpers — parsing
# ---------------------------------------------------------------------------


def _parse_dashboards(databag: dict[str, str]) -> tuple[ValidationCheck, dict[str, Any]]:
    """Validate presence and JSON-decodability of the 'dashboards' key.

    Returns (check, parsed_object). On failure the parsed object is empty.
    """
    raw = databag.get("dashboards", "")
    if not raw:
        return (
            ValidationCheck(name="schema", passed=False, message="Missing required field: 'dashboards'"),
            {},
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return (
            ValidationCheck(name="schema", passed=False, message=f"'dashboards' is not valid JSON: {exc}"),
            {},
        )

    if not isinstance(parsed, dict):
        return (
            ValidationCheck(
                name="schema",
                passed=False,
                message=f"'dashboards' must be a JSON object, got {type(parsed).__name__}",
            ),
            {},
        )

    return ValidationCheck(name="schema", passed=True, message="OK"), parsed


# ---------------------------------------------------------------------------
# Pure helpers — structure
# ---------------------------------------------------------------------------


def _validate_structure(outer: dict[str, Any]) -> ValidationCheck:
    """Check top-level keys and per-template required fields."""
    missing_top = [k for k in ("templates", "uuid") if k not in outer]
    if missing_top:
        return ValidationCheck(
            name="structure",
            passed=False,
            message=f"Missing top-level keys: {', '.join(missing_top)}",
        )

    templates = outer["templates"]
    if not isinstance(templates, dict) or not templates:
        return ValidationCheck(name="structure", passed=False, message="'templates' must be a non-empty object")

    errors: list[str] = []
    for tid, tmpl in templates.items():
        if not isinstance(tmpl, dict):
            errors.append(f"template {tid!r} is not an object")
            continue
        missing = [f for f in _REQUIRED_TEMPLATE_FIELDS if f not in tmpl]
        if missing:
            errors.append(f"template {tid!r} missing: {', '.join(missing)}")

    if errors:
        return ValidationCheck(name="structure", passed=False, message="; ".join(errors))
    return ValidationCheck(name="structure", passed=True, message=f"OK ({len(templates)} template(s))")


# ---------------------------------------------------------------------------
# Pure helpers — content decoding (deep)
# ---------------------------------------------------------------------------


def _validate_content(templates: dict[str, Any]) -> ValidationCheck:
    """Verify each template's 'content' field decodes from LZMA+Base64 to valid JSON."""
    errors: list[str] = []
    for tid, tmpl in templates.items():
        raw_content = tmpl.get("content", "")
        if not raw_content:
            errors.append(f"template {tid!r}: 'content' is empty")
            continue
        try:
            decoded = lzma.decompress(base64.b64decode(raw_content.encode("utf-8")))
            json.loads(decoded)
        except Exception as exc:
            errors.append(f"template {tid!r}: decode failed: {exc}")

    if errors:
        return ValidationCheck(name="content", passed=False, message="; ".join(errors))
    return ValidationCheck(
        name="content", passed=True, message=f"All {len(templates)} template(s) decoded successfully"
    )
