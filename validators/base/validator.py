"""
Base validator implementation for interface validators.

This is production code that will be published as-is in Phase 2 to:
    charmlibs-validators-base

The BaseValidator abstract class enforces a consistent API across all
interface validators, enabling framework-driven discovery and execution.
"""

import time
from abc import ABC, abstractmethod
from typing import List, Literal, Optional

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict


class ValidationCheck(TypedDict):
    """Single validation check result."""

    name: str
    status: Literal["PASS", "FAIL", "ERROR"]
    message: str
    duration_ms: Optional[int]
    error: Optional[str]


class ValidationError(TypedDict):
    """Error details for ERROR status."""

    type: str
    message: str


class ValidationResult(TypedDict):
    """Complete validation result."""

    status: Literal["PASS", "FAIL", "ERROR"]
    interface: str
    level: str
    checks: List[ValidationCheck]
    error: Optional[ValidationError]


class BaseValidator(ABC):
    """Base class for all interface validators.

    The Ops framework discovers validators by convention:
    - Module: charmlibs.validators.{interface_name}.v0.validator
    - Class: {InterfaceName}Validator (must inherit from BaseValidator)

    Subclasses must implement:
    - interface_name (class attribute)
    - _validate_schema() - validate relation data against interface schema
    - _validate_l1() - simple connectivity checks
    - _validate_l2() - deep read/write checks

    Example:
        from charmlibs.interfaces.postgresql_client.v0 import schema
        from validators.base import BaseValidator

        class PostgreSQLClientValidator(BaseValidator):
            interface_name = "postgresql_client"

            def _validate_schema(self, relation_data: dict):
                return schema.PostgreSQLProviderData.parse_obj(relation_data)

            def _validate_l1(self) -> List[ValidationCheck]:
                # L1 checks implementation
                pass

            def _validate_l2(self) -> List[ValidationCheck]:
                # L2 checks implementation
                pass
    """

    interface_name: str  # Must be set by subclass (e.g., "postgresql_client")

    def __init__(self, relation_data: dict):
        """Initialize validator with relation databag.

        Args:
            relation_data: Dictionary from relation.data[app]
        """
        self.relation_data = relation_data

        # Validate schema compliance
        try:
            self.validated_data = self._validate_schema(relation_data)
            self.schema_error = None
        except Exception as e:
            self.schema_error = str(e)
            self.validated_data = None

    @abstractmethod
    def _validate_schema(self, relation_data: dict):
        """Validate relation data against interface schema.

        Must use the interface's schema.py from charmlibs-interfaces-{interface}.

        Args:
            relation_data: Relation databag dictionary

        Returns:
            Parsed schema object (e.g., PostgreSQLProviderData)

        Raises:
            ValidationError: If schema validation fails
        """
        pass

    @abstractmethod
    def _validate_l1(self) -> List[ValidationCheck]:
        """Run L1 (simple) validation checks.

        L1 checks should:
        - Complete in < 5 seconds
        - Be read-only (no mutations)
        - Test: connectivity, authentication, basic queries

        Returns:
            List of validation checks
        """
        pass

    @abstractmethod
    def _validate_l2(self) -> List[ValidationCheck]:
        """Run L2 (deep) validation checks.

        L2 checks should:
        - Complete in < 60 seconds
        - Include L1 checks
        - Test: canary writes, read verification, cleanup

        Returns:
            List of validation checks
        """
        pass

    def validate_integration(self, level: str = "simple") -> ValidationResult:
        """Validate integration health.

        Called by Ops framework during automatic or on-demand validation.

        Args:
            level: "simple" (L1) or "deep" (L2)

        Returns:
            ValidationResult dictionary
        """
        # Check schema validation first
        if self.schema_error:
            return self._error_result(
                level=level,
                error_type="schema_validation_failed",
                error_message=f"Relation data does not match schema: {self.schema_error}",
                checks=[self._failed_check("schema_validation", self.schema_error)],
            )

        # Dispatch to appropriate level
        try:
            if level == "simple":
                checks = self._validate_l1()
            elif level == "deep":
                checks = self._validate_l2()
            else:
                return self._error_result(
                    level=level,
                    error_type="invalid_level",
                    error_message=f"Unknown level: {level}. Must be 'simple' or 'deep'",
                )

            # Determine overall status from checks
            if any(c["status"] == "ERROR" for c in checks):
                status = "ERROR"
            elif any(c["status"] == "FAIL" for c in checks):
                status = "FAIL"
            else:
                status = "PASS"

            return ValidationResult(
                status=status, interface=self.interface_name, level=level, checks=checks, error=None
            )

        except Exception as e:
            return self._error_result(
                level=level, error_type="validator_exception", error_message=f"Unexpected error during validation: {str(e)}"
            )

    # Helper methods to reduce boilerplate

    def _timed_check(self, name: str, check_fn) -> ValidationCheck:
        """Execute a check function and time it.

        Args:
            name: Check name
            check_fn: Function that performs check, returns (status, message)

        Returns:
            ValidationCheck with timing information
        """
        start = time.time()
        try:
            status, message = check_fn()
            duration_ms = int((time.time() - start) * 1000)
            return ValidationCheck(
                name=name,
                status=status,
                message=message,
                duration_ms=duration_ms,
                error=None if status == "PASS" else message,
            )
        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            return ValidationCheck(
                name=name,
                status="ERROR",
                message=f"Check failed with exception: {str(e)}",
                duration_ms=duration_ms,
                error=str(e),
            )

    def _passed_check(self, name: str, message: str = None, duration_ms: int = None) -> ValidationCheck:
        """Create a PASS check result."""
        return ValidationCheck(
            name=name, status="PASS", message=message or f"{name} passed", duration_ms=duration_ms, error=None
        )

    def _failed_check(self, name: str, message: str, duration_ms: int = None) -> ValidationCheck:
        """Create a FAIL check result."""
        return ValidationCheck(name=name, status="FAIL", message=message, duration_ms=duration_ms, error=message)

    def _error_check(self, name: str, error: str, duration_ms: int = None) -> ValidationCheck:
        """Create an ERROR check result."""
        return ValidationCheck(
            name=name, status="ERROR", message=f"Error during {name}: {error}", duration_ms=duration_ms, error=error
        )

    def _error_result(
        self, level: str, error_type: str, error_message: str, checks: List[ValidationCheck] = None
    ) -> ValidationResult:
        """Create an ERROR validation result."""
        return ValidationResult(
            status="ERROR",
            interface=self.interface_name,
            level=level,
            checks=checks or [],
            error=ValidationError(type=error_type, message=error_message),
        )
