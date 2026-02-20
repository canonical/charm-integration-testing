"""
Base validator module for interface validators.

This module will be published as charmlibs-validators-base in Phase 2.

Provides:
- BaseValidator: Abstract base class for all interface validators
- TypedDict definitions: ValidationResult, ValidationCheck, ValidationError
"""

from .validator import BaseValidator, ValidationCheck, ValidationError, ValidationResult

__all__ = ["BaseValidator", "ValidationCheck", "ValidationError", "ValidationResult"]
