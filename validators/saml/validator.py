# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from defusedxml import ElementTree

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

_REQUIRED_FIELDS = (
    "entity_id",
    "single_sign_on_service_redirect_url",
    "single_sign_on_service_redirect_binding",
    "x509certs",
)
_HTTP_TIMEOUT = 10
_SUPPORTED_BINDINGS = {
    "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Artifact",
    "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
    "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
    "urn:oasis:names:tc:SAML:2.0:bindings:PAOS",
    "urn:oasis:names:tc:SAML:2.0:bindings:SOAP",
}


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
                _binding_check(self.databag["single_sign_on_service_redirect_binding"]),
                _certificate_check(self.databag["x509certs"]),
            ]
        )
        if not all(check.passed for check in checks):
            return self._fail_result(level, checks)
        if level == "deep" and self.databag.get("metadata_url"):
            checks.append(_metadata_check(self.databag["metadata_url"]))
        return self._make_result(level=level, checks=checks)


def _url_check(name: str, value: str) -> ValidationCheck:
    try:
        parsed = urlparse(value)
        passed = (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and not any(character.isspace() for character in value)
        )
        _ = parsed.port
    except ValueError:
        passed = False
    return ValidationCheck(
        name=name, passed=passed, message="URL is valid." if passed else f"{name} is not a valid URL."
    )


def _binding_check(value: str) -> ValidationCheck:
    passed = value in _SUPPORTED_BINDINGS
    return ValidationCheck(
        name="binding",
        passed=passed,
        message="SAML binding is supported." if passed else f"Unsupported SAML binding: {value!r}.",
    )


def _certificate_check(value: str) -> ValidationCheck:
    certificates = [certificate.strip() for certificate in value.split(",") if certificate.strip()]
    try:
        passed = bool(certificates) and all(
            x509.load_pem_x509_certificate(certificate.encode(), default_backend()) for certificate in certificates
        )
    except ValueError:
        passed = False
    return ValidationCheck(
        name="certificate",
        passed=passed,
        message="X.509 certificate material is valid."
        if passed
        else "x509certs does not contain valid PEM certificates.",
    )


def _metadata_check(url: str) -> ValidationCheck:
    url_check = _url_check("metadata_url", url)
    if not url_check.passed:
        return ValidationCheck(name="metadata", passed=False, message="metadata_url is not a valid HTTP(S) URL.")
    try:
        request = Request(url, headers={"User-Agent": "charm-integration-testing-saml-validator"})
        with urlopen(request, timeout=_HTTP_TIMEOUT) as response:  # nosec B310 - scheme is checked above
            body = response.read()
        root = ElementTree.fromstring(body)
        if root.tag.rsplit("}", 1)[-1] != "EntityDescriptor":
            return ValidationCheck(
                name="metadata", passed=False, message="SAML metadata response has no EntityDescriptor."
            )
        return ValidationCheck(name="metadata", passed=True, message="SAML metadata is reachable and valid XML.")
    except (ElementTree.ParseError, HTTPError, URLError, OSError) as exc:
        return ValidationCheck(name="metadata", passed=False, message=f"SAML metadata check failed: {exc}.")
