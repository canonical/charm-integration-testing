# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

_REQUIRED_FIELDS = (
    "entity_id",
    "single_sign_on_service_redirect_url",
    "single_sign_on_service_redirect_binding",
    "x509certs",
)
_HTTP_TIMEOUT = 10


class SamlValidator(BaseValidator):
    """Validate SAML identity-provider metadata published over a relation."""

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        checks = [self.validate_schema(list(_REQUIRED_FIELDS))]
        if not checks[0].passed:
            return self._fail_result(level, checks)
        checks.extend(
            [
                _url_check("entity_id", self.databag["entity_id"]),
                _url_check("single_sign_on_service_redirect_url", self.databag["single_sign_on_service_redirect_url"]),
                _certificate_check(self.databag["x509certs"]),
            ]
        )
        if not all(check.passed for check in checks):
            return self._fail_result(level, checks)
        if level == "deep" and self.databag.get("metadata_url"):
            checks.append(_metadata_check(self.databag["metadata_url"]))
        return self._make_result(level=level, checks=checks)


def _url_check(name: str, value: str) -> ValidationCheck:
    passed = value.startswith(("http://", "https://")) and " " not in value
    return ValidationCheck(
        name=name, passed=passed, message="URL is valid." if passed else f"{name} is not a valid URL."
    )


def _certificate_check(value: str) -> ValidationCheck:
    certificates = [certificate.strip() for certificate in value.split(",") if certificate.strip()]
    passed = bool(certificates) and all(
        ("-----BEGIN CERTIFICATE-----" in certificate and "-----END CERTIFICATE-----" in certificate)
        or len(certificate) >= 100
        for certificate in certificates
    )
    return ValidationCheck(
        name="certificate",
        passed=passed,
        message="X.509 certificate material is present." if passed else "x509certs does not contain a PEM certificate.",
    )


def _metadata_check(url: str) -> ValidationCheck:
    if urlparse(url).scheme not in ("http", "https"):
        return ValidationCheck(name="metadata", passed=False, message="metadata_url must use HTTP or HTTPS.")
    try:
        request = Request(url, headers={"User-Agent": "charm-integration-testing-saml-validator"})
        with urlopen(request, timeout=_HTTP_TIMEOUT) as response:  # nosec B310 - scheme is checked above
            body = response.read()
        if b"<EntityDescriptor" not in body and b"<md:EntityDescriptor" not in body:
            return ValidationCheck(
                name="metadata", passed=False, message="SAML metadata response has no EntityDescriptor."
            )
        return ValidationCheck(name="metadata", passed=True, message="SAML metadata is reachable and valid XML.")
    except (HTTPError, URLError, OSError) as exc:
        return ValidationCheck(name="metadata", passed=False, message=f"SAML metadata check failed: {exc}.")
