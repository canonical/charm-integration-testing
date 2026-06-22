# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio

from temporalio.api.workflowservice.v1 import GetSystemInfoRequest
from temporalio.client import Client

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_REQUIRED_FIELDS = ["host", "port"]
_PROBE_TIMEOUT_SECS = 10


class TemporalHostInfoValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level in ("deep", "uat"):
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        if level == "simple":
            return self._validate_simple()
        return self._skipped_result_due_to_level(level)

    def _validate_simple(self) -> ValidationResult:
        """L1: Schema, port format, and Temporal GetSystemInfo RPC."""
        checks: list[ValidationCheck] = []

        schema_check = self.validate_schema(_REQUIRED_FIELDS)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        port_check = self._check_port_is_integer(self.databag["port"])
        checks.append(port_check)
        if not port_check.passed:
            return self._make_result(level="simple", checks=checks)

        host = self.databag["host"]
        port = int(self.databag["port"])
        checks.append(self._check_get_system_info(host, port))

        return self._make_result(level="simple", checks=checks)

    def _check_port_is_integer(self, port_value: str) -> ValidationCheck:
        """Verify the port field is a valid positive integer."""
        try:
            port = int(port_value)
            if port <= 0 or port > 65535:
                raise ValueError(f"Port {port} out of valid range 1-65535.")
            return ValidationCheck(name="port_format", passed=True, message=f"Port {port} is valid.")
        except ValueError as exc:
            return ValidationCheck(name="port_format", passed=False, message=str(exc))

    def _check_get_system_info(self, host: str, port: int) -> ValidationCheck:
        """Call Temporal's GetSystemInfo RPC to confirm the frontend is serving."""
        try:
            asyncio.run(asyncio.wait_for(self._probe_system_info(host, port), timeout=_PROBE_TIMEOUT_SECS))
            return ValidationCheck(
                name="get_system_info",
                passed=True,
                message=f"Temporal frontend at {host}:{port} responded to GetSystemInfo.",
            )
        except asyncio.TimeoutError:
            return ValidationCheck(
                name="get_system_info",
                passed=False,
                message=f"GetSystemInfo timed out after {_PROBE_TIMEOUT_SECS}s ({host}:{port}).",
            )
        except Exception as exc:
            return ValidationCheck(
                name="get_system_info",
                passed=False,
                message=f"GetSystemInfo failed: {exc}",
            )

    @staticmethod
    async def _probe_system_info(host: str, port: int) -> None:
        """Connect to the Temporal frontend and issue a GetSystemInfo request.

        The temporalio SDK does not expose an explicit close method on Client or
        ServiceClient. Using ``del`` drops the reference immediately so CPython's
        reference counting releases the underlying gRPC channel without waiting
        for the next GC cycle.
        """
        client = await Client.connect(f"{host}:{port}", tls=False)
        try:
            await client.service_client.workflow_service.get_system_info(GetSystemInfoRequest())
        finally:
            del client  # release gRPC channel; SDK has no explicit close()
