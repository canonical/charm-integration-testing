# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import socket

import grpc  # type: ignore[import-untyped]
from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

# gRPC channel-ready timeout in seconds.
_GRPC_READY_TIMEOUT = 5.0
# TCP-ping timeout in seconds.
_TCP_TIMEOUT = 5.0


class ParcaStoreValidator(BaseValidator):
    """Validator for the ``parca_store`` Juju interface.

    The provider side of this interface exposes a Parca profile-store gRPC
    endpoint.  The requirer connects to it to forward profiling data.

    Validation levels:
      * simple (L1): schema correctness, endpoint parse, TCP reachability,
        and TLS prerequisites.
      * deep   (L2): establish a real gRPC channel and confirm the transport
        layer is fully negotiated.
    """

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level == "simple":
            return self._validate_simple()
        if level == "deep":
            return self._validate_deep()
        return self._skipped_result_due_to_level(level)

    # ------------------------------------------------------------------
    # Private — validation levels
    # ------------------------------------------------------------------

    def _validate_simple(self) -> ValidationResult:
        """L1: Schema correctness, endpoint parse, TCP reachability."""
        checks: list[ValidationCheck] = []

        if not self.relation_exists():
            return self._error_result("simple", f"No remote application on relation '{self.endpoint}'.")

        # 1. Schema: remote-store-address must be present.
        schema_check = self.validate_schema(["remote-store-address"])
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        address = self.databag["remote-store-address"]
        insecure = self.databag.get("remote-store-insecure", "false").lower() == "true"

        # 2. Parse the gRPC address into host:port.
        parse_check, host, port = _parse_grpc_address(address)
        checks.append(parse_check)
        if not parse_check.passed:
            return self._make_result(level="simple", checks=checks)

        # 3. TCP reachability.
        checks.append(_tcp_ping_check(host, port))

        # 4. TLS prerequisite: if insecure=false the port must speak TLS.
        if not insecure:
            checks.append(_tls_prerequisite_check(host, port))

        return self._make_result(level="simple", checks=checks)

    def _validate_deep(self) -> ValidationResult:
        """L2: Establish a real gRPC channel and confirm transport is ready."""
        checks: list[ValidationCheck] = []

        if not self.relation_exists():
            return self._error_result("deep", f"No remote application on relation '{self.endpoint}'.")

        # 1. Schema: remote-store-address must be present.
        schema_check = self.validate_schema(["remote-store-address"])
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        address = self.databag["remote-store-address"]
        insecure = self.databag.get("remote-store-insecure", "false").lower() == "true"
        token = self.databag.get("remote-store-bearer-token", "")

        # 2. Parse the gRPC address.
        parse_check, host, port = _parse_grpc_address(address)
        checks.append(parse_check)
        if not parse_check.passed:
            return self._make_result(level="deep", checks=checks)

        # 3. TCP reachability (fast-fail before gRPC dial).
        tcp_check = _tcp_ping_check(host, port)
        checks.append(tcp_check)
        if not tcp_check.passed:
            return self._make_result(level="deep", checks=checks)

        # 4. gRPC channel ready — proves transport layer negotiates correctly.
        checks.append(_grpc_channel_ready_check(address, insecure=insecure, token=token))

        return self._make_result(level="deep", checks=checks)


# ------------------------------------------------------------------
# Pure helpers
# ------------------------------------------------------------------


def _parse_grpc_address(address: str) -> tuple[ValidationCheck, str, int]:
    """Parse a bare ``host:port`` gRPC address.

    Returns *(check, host, port)*.  On failure *host* and *port* are empty/0.
    gRPC addresses for the ``parca_store`` interface intentionally have no
    scheme (e.g. ``10.1.2.3:7070`` or ``parca.namespace.svc:7070``).
    """
    try:
        # Strip any accidental scheme prefix.
        stripped = address.split("://")[-1]
        if ":" not in stripped:
            return (
                ValidationCheck(
                    name="parse",
                    passed=False,
                    message=f"Address '{address}' has no port component.",
                ),
                "",
                0,
            )
        host, port_str = stripped.rsplit(":", 1)
        port = int(port_str)
        if not host:
            raise ValueError("empty host")
        if not (1 <= port <= 65535):
            raise ValueError(f"port {port} out of range")
    except Exception as exc:
        return (
            ValidationCheck(name="parse", passed=False, message=f"Cannot parse gRPC address '{address}': {exc}"),
            "",
            0,
        )
    return ValidationCheck(name="parse", passed=True, message=f"Parsed as {host}:{port}."), host, port


def _tcp_ping_check(host: str, port: int) -> ValidationCheck:
    """Open a TCP connection to *host*:*port* and immediately close it."""
    try:
        with socket.create_connection((host, port), timeout=_TCP_TIMEOUT):
            pass
        return ValidationCheck(name="connect", passed=True, message=f"TCP connection to {host}:{port} succeeded.")
    except OSError as exc:
        return ValidationCheck(name="connect", passed=False, message=f"TCP connection to {host}:{port} failed: {exc}")


def _tls_prerequisite_check(host: str, port: int) -> ValidationCheck:
    """Verify that the endpoint presents a TLS handshake when insecure=false."""
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=_TCP_TIMEOUT) as raw:
            with ctx.wrap_socket(raw, server_hostname=host):
                pass
        return ValidationCheck(name="tls", passed=True, message="TLS handshake succeeded.")
    except ssl.SSLError as exc:
        return ValidationCheck(name="tls", passed=False, message=f"TLS handshake failed: {exc}")
    except OSError as exc:
        return ValidationCheck(name="tls", passed=False, message=f"TLS prerequisite check failed: {exc}")


def _grpc_channel_ready_check(address: str, *, insecure: bool, token: str) -> ValidationCheck:
    """Dial the gRPC endpoint and wait for the channel to enter READY state."""
    channel: grpc.Channel | None = None
    try:
        if insecure:
            channel = grpc.insecure_channel(address)
        else:
            credentials = grpc.ssl_channel_credentials()
            if token:
                call_credentials = grpc.access_token_call_credentials(token)
                credentials = grpc.composite_channel_credentials(credentials, call_credentials)
            channel = grpc.secure_channel(address, credentials)

        grpc.channel_ready_future(channel).result(timeout=_GRPC_READY_TIMEOUT)
        return ValidationCheck(name="grpc_ready", passed=True, message=f"gRPC channel to {address} is READY.")
    except grpc.FutureTimeoutError:
        return ValidationCheck(
            name="grpc_ready",
            passed=False,
            message=f"gRPC channel to {address} did not reach READY within {_GRPC_READY_TIMEOUT}s.",
        )
    except Exception as exc:
        return ValidationCheck(name="grpc_ready", passed=False, message=f"gRPC channel error: {exc}")
    finally:
        if channel is not None:
            channel.close()
