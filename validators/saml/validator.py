# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import base64
import binascii
import http.client
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from defusedxml import ElementTree  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

_REQUIRED_FIELDS = (
    "entity_id",
    "single_sign_on_service_redirect_url",
    "single_sign_on_service_redirect_binding",
    "x509certs",
)
_HTTP_TIMEOUT = 10
_MAX_METADATA_BYTES = 1024 * 1024
_REDIRECT_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
_SAML_METADATA_NAMESPACE = "urn:oasis:names:tc:SAML:2.0:metadata"


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_HTTP_OPENER = build_opener(_NoRedirectHandler())


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
                _entity_id_check(self.databag["entity_id"]),
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


def _entity_id_check(value: str) -> ValidationCheck:
    try:
        parsed = urlparse(value)
        passed = (
            bool(value)
            and bool(parsed.scheme)
            and not any(character.isspace() for character in value)
            and bool(parsed.netloc or parsed.path or parsed.params or parsed.query or parsed.fragment)
            and (parsed.scheme not in ("http", "https") or bool(parsed.hostname))
        )
        if parsed.scheme in ("http", "https"):
            _ = parsed.port
    except ValueError:
        passed = False
    return ValidationCheck(
        name="entity_id",
        passed=passed,
        message="Entity ID is valid." if passed else "entity_id is not a valid URI.",
    )


def _binding_check(value: str) -> ValidationCheck:
    passed = value == _REDIRECT_BINDING
    return ValidationCheck(
        name="binding",
        passed=passed,
        message="SAML binding is supported." if passed else f"Unsupported SAML binding: {value!r}.",
    )


def _certificate_check(value: str) -> ValidationCheck:
    certificates = [certificate.strip() for certificate in value.split(",") if certificate.strip()]
    try:
        passed = bool(certificates) and all(_load_certificate(certificate) for certificate in certificates)
    except (ValueError, binascii.Error):
        passed = False
    return ValidationCheck(
        name="certificate",
        passed=passed,
        message="X.509 certificate material is valid."
        if passed
        else "x509certs does not contain valid certificate material.",
    )


def _load_certificate(value: str) -> x509.Certificate:
    if value.startswith("-----BEGIN CERTIFICATE-----"):
        return x509.load_pem_x509_certificate(value.encode(), default_backend())
    der = base64.b64decode("".join(value.split()), validate=True)
    return x509.load_der_x509_certificate(der, default_backend())


def _metadata_check(url: str) -> ValidationCheck:
    url_check = _url_check("metadata_url", url)
    if not url_check.passed:
        return ValidationCheck(name="metadata", passed=False, message="metadata_url is not a valid HTTP(S) URL.")
    try:
        request = Request(url, headers={"User-Agent": "charm-integration-testing-saml-validator"})
        with _HTTP_OPENER.open(request, timeout=_HTTP_TIMEOUT) as response:  # nosec B310 - scheme is checked above
            body = response.read(_MAX_METADATA_BYTES + 1)
        if len(body) > _MAX_METADATA_BYTES:
            return ValidationCheck(name="metadata", passed=False, message="SAML metadata response is too large.")
        root = ElementTree.fromstring(body)
        if root.tag != f"{{{_SAML_METADATA_NAMESPACE}}}EntityDescriptor":
            return ValidationCheck(
                name="metadata", passed=False, message="SAML metadata response has no EntityDescriptor."
            )
        return ValidationCheck(name="metadata", passed=True, message="SAML metadata is reachable and valid XML.")
    except (
        DefusedXmlException,
        ElementTree.ParseError,
        HTTPError,
        URLError,
        http.client.HTTPException,
        OSError,
    ) as exc:
        return ValidationCheck(name="metadata", passed=False, message=f"SAML metadata check failed: {exc}.")
