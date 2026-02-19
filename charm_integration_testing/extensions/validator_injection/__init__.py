# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""
Validator injection module for Phase 1 testing.

This module will be DELETED in Phase 2 when the Ops framework provides native support.
"""

from .extension import ValidatorInjectorExtension
from .models import (
    ApplicationValidationResult,
    ModelValidationResult,
    ValidationCheck,
    ValidationError,
    ValidationFailureError,
    ValidationResult,
)

__all__ = [
    "ValidatorInjectorExtension",
    "ValidationCheck",
    "ValidationError",
    "ValidationResult",
    "ApplicationValidationResult",
    "ModelValidationResult",
    "ValidationFailureError",
]
