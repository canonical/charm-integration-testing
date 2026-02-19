# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Data models for validator injection results."""

from typing import Literal

from pydantic.dataclasses import dataclass


@dataclass(frozen=True)
class ValidationCheck:
    """Single validation check result."""

    name: str
    status: Literal["PASS", "FAIL", "ERROR"]
    message: str
    duration_ms: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class ValidationError:
    """Error details for ERROR status."""

    type: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """Complete validation result for a single relation."""

    status: Literal["PASS", "FAIL", "ERROR"]
    interface: str
    level: str
    checks: list[ValidationCheck]
    error: ValidationError | None = None


@dataclass(frozen=True)
class ApplicationValidationResult:
    """Validation results for all relations of an application."""

    application: str
    relations: dict[str, ValidationResult]  # Keyed by relation name
    overall_status: Literal["PASS", "FAIL", "ERROR"]


@dataclass(frozen=True)
class ModelValidationResult:
    """Validation results for entire model."""

    model: str
    applications: dict[str, ApplicationValidationResult]  # Keyed by application name
    overall_status: Literal["PASS", "FAIL", "ERROR"]


class ValidationFailureError(Exception):
    """Raised when validation fails."""

    result: ModelValidationResult

    def __init__(self, result: ModelValidationResult):
        self.result = result
        failed_apps = [
            app for app, app_result in result.applications.items() if app_result.overall_status in ("FAIL", "ERROR")
        ]
        super().__init__(f"Validation failed for applications: {', '.join(failed_apps)}")
