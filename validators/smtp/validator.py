# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import smtplib
import ssl

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

_REQUIRED_FIELDS = ("host", "port", "auth_type", "transport_security")
_AUTH_TYPES = {"none", "not_provided", "plain"}
_TRANSPORT_SECURITY = {"none", "starttls", "tls"}
_SMTP_TIMEOUT = 10


class SmtpValidator(BaseValidator):
    """Validate the provider data and SMTP handshake for the smtp interface."""

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        databag = self.databag
        schema_check = self.validate_schema(list(_REQUIRED_FIELDS))
        checks = [schema_check]
        if not schema_check.passed:
            return self._fail_result(level, checks)

        checks.extend(
            [
                _enum_check("auth_type", databag["auth_type"], _AUTH_TYPES),
                _enum_check("transport_security", databag["transport_security"], _TRANSPORT_SECURITY),
                _port_check(databag["port"]),
            ]
        )
        if not all(check.passed for check in checks):
            return self._fail_result(level, checks)

        if databag["auth_type"] == "plain":
            try:
                credentials = self.resolve_secret("password_id", "user", "password")
            except Exception as exc:
                checks.append(
                    ValidationCheck(
                        name="credentials",
                        passed=False,
                        message=f"Could not resolve SMTP credentials: {exc}.",
                    )
                )
                return self._fail_result(level, checks)
            credential_check = self.validate_schema(["user", "password"], data=credentials)
            credential_check.name = "credentials"
            checks.append(credential_check)
            if not credential_check.passed:
                return self._fail_result(level, checks)

        if level == "deep":
            checks.append(_smtp_handshake_check(databag))

        return self._make_result(level=level, checks=checks)


def _enum_check(name: str, value: str, allowed: set[str]) -> ValidationCheck:
    if value in allowed:
        return ValidationCheck(name=name, passed=True, message=f"{name} {value!r} is valid.")
    return ValidationCheck(
        name=name,
        passed=False,
        message=f"{name} {value!r} is invalid; expected one of {', '.join(sorted(allowed))}.",
    )


def _port_check(value: str) -> ValidationCheck:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return ValidationCheck(name="port", passed=False, message=f"port {value!r} is not an integer.")
    if not 1 <= port <= 65535:
        return ValidationCheck(name="port", passed=False, message=f"port {port} is outside 1-65535.")
    return ValidationCheck(name="port", passed=True, message=f"port {port} is valid.")


def _smtp_handshake_check(databag: dict[str, str]) -> ValidationCheck:
    host = databag["host"]
    port = int(databag["port"])
    security = databag["transport_security"]
    skip_ssl_verify = databag.get("skip_ssl_verify", "").lower() in {"1", "true", "yes", "on"}
    ssl_context = _ssl_context(skip_ssl_verify)
    client: smtplib.SMTP | smtplib.SMTP_SSL
    try:
        if security == "tls":
            client = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT, context=ssl_context)
        else:
            client = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT)
        with client:
            _require_smtp_response(client.ehlo(), 250, "EHLO")
            if security == "starttls":
                _require_smtp_response(client.starttls(context=ssl_context), 220, "STARTTLS")
                _require_smtp_response(client.ehlo(), 250, "EHLO after STARTTLS")
        return ValidationCheck(name="smtp_handshake", passed=True, message="SMTP handshake succeeded.")
    except (OSError, smtplib.SMTPException) as exc:
        return ValidationCheck(name="smtp_handshake", passed=False, message=f"SMTP handshake failed: {exc}.")


def _ssl_context(skip_ssl_verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if skip_ssl_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _require_smtp_response(response: tuple[int, bytes], expected: int, command: str) -> None:
    code, message = response
    if code != expected:
        raise smtplib.SMTPException(f"{command} returned {code}: {message!r}")
