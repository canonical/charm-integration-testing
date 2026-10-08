# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Reusable validators for tests of the shared orchestration packages."""

from validators.base import BaseValidator, ValidationLevel, ValidationResult


class PassingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return self._make_result(level=level, status="PASS")


class FailingValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return self._make_result(level=level, status="FAIL", error="database not reachable")


class ErroringValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return self._make_result(level=level, status="ERROR", error="boom")
