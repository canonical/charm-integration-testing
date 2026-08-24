# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import socket
from typing import Any

import yaml  # pyyaml; required to decode the serialized-data-interface (SDI) "data" wire format

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

_REQUIRED_UNIT_FIELDS = ("ingress-address", "private-address", "egress-subnets")
_REQUIRED_PROVIDER_FIELDS = ("service", "port")
_OPTIONAL_HEADER_FIELDS = ("allowed-request-headers", "allowed-response-headers")
_TCP_TIMEOUT = 5


class IngressAuthValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if level == "uat":
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        if self.role == "provides":
            return self._validate_provides(level)
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level != "simple":
            return self._skipped_result_due_to_level(level)

        # Resolve optional credentials if this provider chooses secret-backed fields.
        self.resolve_secret("secret-auth")

        required_app_fields = ["_supported_versions"] if "_supported_versions" in self.databag else []
        checks: list[ValidationCheck] = [self.validate_schema(required_app_fields)]
        checks.append(self._validate_remote_unit_databags())
        return self._make_result(level=level, checks=checks)

    def _validate_provides(self, level: ValidationLevel) -> ValidationResult:
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        schema_check, data = self._decode_requirer_data()
        checks: list[ValidationCheck] = [schema_check]
        if not schema_check.passed:
            return self._make_result(level=level, checks=checks)

        fields_check = _check_required_fields(data)
        checks.append(fields_check)
        if not fields_check.passed:
            return self._make_result(level=level, checks=checks)

        port_check = _check_port(data["port"])
        checks.append(port_check)
        for field in _OPTIONAL_HEADER_FIELDS:
            if field in data:
                checks.append(_check_header_list(field, data[field]))

        if level == "deep" and port_check.passed:
            checks.append(self._validate_connectivity(str(data["service"]), int(data["port"])))

        return self._make_result(level=level, checks=checks)

    def _decode_requirer_data(self) -> tuple[ValidationCheck, dict[str, Any]]:
        """Decode the requirer's payload from the SDI (serialized-data-interface) wire format.

        The ``serialized_data_interface`` library used by oidc-gatekeeper and istio-pilot
        serializes the entire payload dict as YAML under a single ``data`` key in the app
        databag, rather than as flat top-level fields.
        """
        raw = self.databag.get("data", "")
        if not raw:
            return (
                ValidationCheck(name="schema", passed=False, message="Missing 'data' key in provider app databag."),
                {},
            )
        try:
            decoded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            return (
                ValidationCheck(name="schema", passed=False, message=f"Could not decode 'data' field as YAML: {exc}"),
                {},
            )
        if not isinstance(decoded, dict):
            return (
                ValidationCheck(
                    name="schema",
                    passed=False,
                    message=f"'data' field decoded to {type(decoded).__name__}, expected a mapping.",
                ),
                {},
            )
        return ValidationCheck(name="schema", passed=True, message="OK"), decoded

    def _validate_connectivity(self, service: str, port: int) -> ValidationCheck:
        """TCP-probe the requirer's advertised authservice at ``service:port``.

        The provider (e.g. istio-pilot) runs in the same Kubernetes namespace
        as the requirer, so the in-cluster service DNS name is reachable.
        """
        host = f"{service}.{self.charm.model.name}.svc.cluster.local"
        try:
            with socket.create_connection((host, port), timeout=_TCP_TIMEOUT):
                pass
            return ValidationCheck(
                name="connectivity",
                passed=True,
                message=f"TCP reached {host}:{port}.",
            )
        except OSError as exc:
            return ValidationCheck(
                name="connectivity",
                passed=False,
                message=f"TCP connection to {host}:{port} failed: {exc}",
            )

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


def _check_required_fields(data: dict[str, Any]) -> ValidationCheck:
    missing = [field for field in _REQUIRED_PROVIDER_FIELDS if not data.get(field)]
    return ValidationCheck(
        name="required_fields",
        passed=not missing,
        message="OK" if not missing else f"Missing: {', '.join(missing)}",
    )


def _check_port(port_value: Any) -> ValidationCheck:
    try:
        port = int(port_value)
    except (TypeError, ValueError):
        return ValidationCheck(name="port", passed=False, message="'port' must be an integer.")
    if not 1 <= port <= 65535:
        return ValidationCheck(name="port", passed=False, message="'port' must be between 1 and 65535.")
    return ValidationCheck(name="port", passed=True, message=f"Valid port {port}.")


def _check_header_list(field: str, value: Any) -> ValidationCheck:
    valid = isinstance(value, list) and all(isinstance(header, str) and header for header in value)
    return ValidationCheck(
        name=field,
        passed=valid,
        message=f"'{field}' must be a list of non-empty strings." if not valid else f"Validated {field}.",
    )
