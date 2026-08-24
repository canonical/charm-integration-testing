# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import ssl
from datetime import datetime, timezone

from cryptography import x509

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)


class CertificateTransferValidator(BaseValidator):
    """Validator for the certificate_transfer interface.

    Certificates arrive either in the provider's application databag as a
    JSON-encoded list under ``certificates`` (interface v1), or per related
    unit as JSON-encoded ``ca``/``certificate``/``chain`` fields (interface
    v0 fallback, negotiated when the requirer does not hint v1 support).
    """

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)

        if level == "uat":
            return self._skipped_result_due_to_level(level)

        error_result = self._check_relation_exists(level)
        if error_result:
            return error_result

        certificates_pem, schema_check = self._collect_certificates()

        if level == "simple":
            return self._validate_simple(certificates_pem, schema_check)
        elif level == "deep":
            return self._validate_deep(certificates_pem, schema_check)
        return self._skipped_result_due_to_level(level)

    def _validate_simple(self, certificates_pem: list[str], schema_check: ValidationCheck) -> ValidationResult:
        """L1: relation schema, X.509 structure, and certificate validity period."""
        checks = [schema_check]
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        parsed, parseable_check = self._parse_certificates(certificates_pem)
        checks.append(parseable_check)
        if not parseable_check.passed:
            return self._make_result(level="simple", checks=checks)

        checks.append(self._check_not_expired(parsed))
        return self._make_result(level="simple", checks=checks)

    def _validate_deep(self, certificates_pem: list[str], schema_check: ValidationCheck) -> ValidationResult:
        """L2: canary interaction that loads transferred certificates into a real TLS trust store."""
        checks = [schema_check]
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        parsed, parseable_check = self._parse_certificates(certificates_pem)
        checks.append(parseable_check)
        if not parseable_check.passed:
            return self._make_result(level="deep", checks=checks)

        checks.append(self._check_not_expired(parsed))
        checks.append(self._check_trust_store_usable(certificates_pem))
        return self._make_result(level="deep", checks=checks)

    def _check_relation_exists(self, level: ValidationLevel) -> ValidationResult | None:
        """Return an ERROR result if the remote app is absent, else None."""
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        return None

    def _collect_certificates(self) -> tuple[list[str], ValidationCheck]:
        """Collect transferred certificate PEM strings and dedupe them.

        Reads the v1 provider app databag ``certificates`` field and falls back to
        the v0 per-unit ``ca``/``certificate``/``chain`` fields, since providers
        negotiate the interface version based on the requirer's declared support.
        """
        certificates: list[str] = []
        decode_errors: list[str] = []

        raw_certificates = self.databag.get("certificates")
        if raw_certificates:
            certs, error = self._decode_string_list(raw_certificates, field="certificates")
            if error:
                decode_errors.append(error)
            else:
                certificates.extend(certs)

        for unit in self.relation.units:
            unit_data = dict(self.relation.data[unit])
            for field in ("certificate", "ca"):
                raw_value = unit_data.get(field)
                if raw_value:
                    certificates.append(self._decode_v0_field(raw_value))
            raw_chain = unit_data.get("chain")
            if raw_chain:
                chain, error = self._decode_string_list(raw_chain, field="chain")
                if error:
                    decode_errors.append(error)
                else:
                    certificates.extend(chain)

        deduped = list(dict.fromkeys(certificates))

        if decode_errors:
            return deduped, ValidationCheck(name="schema", passed=False, message="; ".join(decode_errors))
        if not deduped:
            return deduped, ValidationCheck(
                name="schema",
                passed=False,
                message=(
                    "No certificates found in provider app databag ('certificates') "
                    "or unit databags ('certificate'/'ca'/'chain')."
                ),
            )
        return deduped, ValidationCheck(name="schema", passed=True, message=f"Found {len(deduped)} certificate(s).")

    @staticmethod
    def _decode_string_list(raw_value: str, *, field: str) -> tuple[list[str], str | None]:
        """Decode a JSON field expected to hold a list of strings.

        Returns the decoded list and no error on success, or an empty list and a
        descriptive schema error if the value isn't valid JSON or isn't a list of
        strings.
        """
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            return [], f"Invalid JSON in '{field}' field: {exc}"
        if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
            return [], f"Expected '{field}' field to decode to a list of strings, got: {decoded!r}"
        return decoded, None

    @staticmethod
    def _decode_v0_field(raw_value: str) -> str:
        """Decode a unit databag field, tolerating plain (non-JSON) PEM strings.

        Falls back to the raw string when the value isn't valid JSON or doesn't
        decode to a string.
        """
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError:
            return raw_value
        return decoded if isinstance(decoded, str) else raw_value

    def _parse_certificates(self, certificates_pem: list[str]) -> tuple[list[x509.Certificate], ValidationCheck]:
        """Parse each PEM string as an X.509 certificate."""
        parsed: list[x509.Certificate] = []
        failures: list[str] = []
        for pem in certificates_pem:
            try:
                parsed.append(x509.load_pem_x509_certificate(pem.encode()))
            except ValueError as exc:
                failures.append(str(exc))

        if failures:
            return parsed, ValidationCheck(
                name="parseable",
                passed=False,
                message=f"Failed to parse {len(failures)}/{len(certificates_pem)} certificate(s): "
                f"{'; '.join(failures)}",
            )
        return parsed, ValidationCheck(
            name="parseable",
            passed=True,
            message=f"All {len(parsed)} certificate(s) are valid X.509 PEM.",
        )

    def _check_not_expired(self, certificates: list[x509.Certificate]) -> ValidationCheck:
        """Verify all transferred certificates are currently within their validity period."""
        now = datetime.now(timezone.utc)
        out_of_range = [
            cert for cert in certificates if cert.not_valid_after_utc < now or cert.not_valid_before_utc > now
        ]
        if out_of_range:
            return ValidationCheck(
                name="validity_period",
                passed=False,
                message=f"{len(out_of_range)}/{len(certificates)} certificate(s) are expired or not yet valid.",
            )
        return ValidationCheck(
            name="validity_period",
            passed=True,
            message=f"All {len(certificates)} certificate(s) are within their validity period.",
        )

    def _check_trust_store_usable(self, certificates_pem: list[str]) -> ValidationCheck:
        """Canary: load the transferred certificates into a real TLS trust store.

        This exercises the actual consumption path a requirer charm relies on:
        using the transferred certificate material as trust anchors for TLS
        verification via the standard library's SSL stack.
        """
        try:
            context = ssl.create_default_context()
            for pem in certificates_pem:
                context.load_verify_locations(cadata=pem)
        except ssl.SSLError as exc:
            return ValidationCheck(name="trust_store_load", passed=False, message=str(exc))
        return ValidationCheck(
            name="trust_store_load",
            passed=True,
            message=f"Loaded {len(certificates_pem)} certificate(s) into a TLS trust store.",
        )
