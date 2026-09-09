# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import socket
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import grpc
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_SIMPLE_LATENCY_TARGET_S = 0.5
_DEEP_LATENCY_TARGET_S = 10.0
_TCP_CONNECT_TIMEOUT_S = 3.0
_GRPC_TIMEOUT_S = 5.0

# etcd's client port serves both native gRPC and a JSON grpc-gateway. The
# grpc-gateway is deliberately unusable here: etcd rejects gateway writes made
# under CommonName-based client-cert auth ("CommonName ... will be ignored and
# not used as expected"), since the gateway can't safely forward the peer TLS
# identity. So L2 talks to etcd's KV service via real gRPC instead. Rather than
# depend on generated protobuf stubs (etcd's own client libraries are either
# unmaintained or pin old protobuf versions), these are minimal hand-rolled
# encoders/decoders for the tiny subset of the KV service's wire format this
# validator needs (Put/Range/DeleteRange: only string/bytes fields).
_KV_SERVICE = "etcdserverpb.KV"


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7


def _encode_bytes_field(field_number: int, value: bytes) -> bytes:
    tag = (field_number << 3) | 2  # wire type 2: length-delimited
    return _encode_varint(tag) + _encode_varint(len(value)) + value


def _decode_message(data: bytes) -> dict[int, list[bytes | int]]:
    """Decode a protobuf message into a map of field number -> list of raw values.

    Only handles wire types 0 (varint) and 2 (length-delimited), which covers
    every field this validator reads from etcd's KV responses.
    """
    fields: dict[int, list[bytes | int]] = {}
    pos = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        field_number, wire_type = tag >> 3, tag & 0x7
        if wire_type == 2:
            length, pos = _decode_varint(data, pos)
            value: bytes | int = data[pos : pos + length]
            pos += length
        elif wire_type == 0:
            value, pos = _decode_varint(data, pos)
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        fields.setdefault(field_number, []).append(value)
    return fields


# Conventional, out-of-band location for a client cert/private-key pair matching
# whatever is published on the requirer's own "mtls-cert" field. The etcd_client
# interface deliberately never transmits private keys over Juju relations (auth is
# mTLS-only), so a generic validator has no interface-level way to obtain one; it
# must be provisioned onto the unit by whoever deploys it, at this documented path
# (overridable via env vars). This mirrors how canonical's own reference test
# fixtures for this interface supply client identity: from local files, not relation
# data. See ETCD_CLIENT_CERT_PATH_ENV / ETCD_CLIENT_KEY_PATH_ENV.
_DEFAULT_CLIENT_CERT_PATH = "/etc/validators/etcd-client/client.pem"
_DEFAULT_CLIENT_KEY_PATH = "/etc/validators/etcd-client/client.key"
ETCD_CLIENT_CERT_PATH_ENV = "VALIDATOR_ETCD_CLIENT_CERT_PATH"
ETCD_CLIENT_KEY_PATH_ENV = "VALIDATOR_ETCD_CLIENT_KEY_PATH"

_REQUIRER_FIELDS = ["endpoints", "uris", "username", "tls-ca", "version"]
_PROVIDER_FIELDS = ["prefix", "mtls-cert"]


class EtcdClientValidator(BaseValidator):
    """Validator for the etcd_client interface.

    Field names differ from a generic "connection string / client cert / client
    key" model: the provider publishes ``endpoints``, ``uris``, ``username``,
    ``tls-ca`` (secret group "tls") and ``version``; there is no password field,
    since auth is mutual TLS only. The requirer publishes ``prefix`` (the
    requested key-prefix) and its own client certificate under ``mtls-cert``
    (secret group "mtls" in deployments that publish it as a secret; some
    library revisions instead send it as a plaintext field, so both forms are
    resolved). The matching private key is never conveyed over the relation by
    design and must be supplied out-of-band (see ``ETCD_CLIENT_KEY_PATH_ENV``)
    for live connectivity/read-write checks.
    """

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)
        if self.role == "requires":
            return self._validate_requires(level)
        if self.role == "provides":
            return self._validate_provides(level)
        return self._skipped_result_due_to_role(level, self.role)

    # --- requires role: validating against a real etcd_client provider ---

    def _validate_requires(self, level: ValidationLevel) -> ValidationResult:
        checks: list[ValidationCheck] = []

        error_result = self._check_relation_exists(level)
        if error_result:
            return error_result

        creds = self._resolve_requirer_side_credentials()
        schema_check = self.validate_schema(_REQUIRER_FIELDS, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level=level, checks=checks)

        data = self.databag | creds

        endpoints_check = self._check_endpoints_format(data["endpoints"])
        checks.append(endpoints_check)
        if not endpoints_check.passed:
            return self._make_result(level=level, checks=checks)

        tls_ca_check, ca_cert = self._parse_tls_ca(data["tls-ca"])
        checks.append(tls_ca_check)
        if not tls_ca_check.passed:
            return self._make_result(level=level, checks=checks)
        checks.append(self._check_not_expired(ca_cert, check_name="tls_ca_validity_period"))

        # Validated at L1 too (not just before the L2 gRPC probe): "uris" is the
        # address the deep probe connects to, so a malformed value must fail
        # simple-level validation rather than only surfacing once deep validation
        # is attempted.
        target, target_check = self._pick_grpc_target(data["uris"])
        checks.append(target_check)
        if not target_check.passed:
            return self._make_result(level=level, checks=checks)

        # Latency is timed from here, not from the top of the function, so that Juju
        # secret/relation-data resolution above - which can be slow for cross-model
        # relations independent of etcd itself - is not counted against the probe's
        # latency budget (see validators/postgresql_client/validator.py for the same
        # pattern).
        start_time = time.monotonic()
        if level == "simple":
            checks.append(self._check_tcp_reachable(data["endpoints"]))
        else:
            checks.extend(self._check_read_write(data, target))

        elapsed = time.monotonic() - start_time
        latency_target = _SIMPLE_LATENCY_TARGET_S if level == "simple" else _DEEP_LATENCY_TARGET_S
        checks.append(self._check_latency(elapsed, latency_target))

        return self._make_result(level=level, checks=checks)

    def _resolve_requirer_side_credentials(self) -> dict[str, str]:
        """Resolve provider-published fields, including secret-backed groups."""
        return {
            **self.resolve_secret("secret-user", "username", "uris"),
            **self.resolve_secret("secret-tls", "tls", "tls-ca"),
        }

    def _check_tcp_reachable(self, endpoints: str) -> ValidationCheck:
        """Best-effort L1 reachability: open a raw TCP connection to the first endpoint.

        A full mTLS handshake requires a client cert/key pair, which the interface
        never conveys (see module docstring), so this check is scoped to basic
        reachability rather than a completed TLS handshake.
        """
        first = endpoints.split(",")[0].strip()
        host, _, port_str = first.rpartition(":")
        # socket.create_connection expects a bare IPv6 address (no brackets), while
        # endpoints/uris use bracketed literals (e.g. "[::1]:2379") for disambiguation.
        host = host.removeprefix("[").removesuffix("]")
        try:
            with socket.create_connection((host, int(port_str)), timeout=_TCP_CONNECT_TIMEOUT_S):
                return ValidationCheck(name="connect", passed=True, message=f"TCP connection to '{first}' succeeded.")
        except OSError as exc:
            return ValidationCheck(name="connect", passed=False, message=f"Could not reach '{first}': {exc}")

    def _check_read_write(self, data: dict[str, str], target: str) -> list[ValidationCheck]:
        """L2: mTLS PUT/GET/DELETE of a canary key using a locally-provisioned client identity.

        ``target`` is the gRPC "host:port" authority already parsed (and format-checked
        at L1) by the caller from ``data["uris"]``.
        """
        checks: list[ValidationCheck] = []

        cert_path, key_path, identity_check = self._resolve_client_identity()
        checks.append(identity_check)
        if not identity_check.passed:
            return checks

        try:
            with open(cert_path, "rb") as fh:
                cert_bytes = fh.read()
            with open(key_path, "rb") as fh:
                key_bytes = fh.read()
        except OSError as exc:
            checks.append(ValidationCheck(name="put", passed=False, message=f"Could not read client identity: {exc}"))
            return checks

        # The requirer's own contribution to this relation (its "prefix" ACL grant
        # and its published mtls-cert) lives on this application's own databag, not
        # on the remote provider's databag (`data`, used above for endpoints/uris/
        # tls-ca): see BaseValidator.databag and _local_databag.
        local_data = self._local_databag()

        identity_match_check = self._check_identity_matches_published_cert(cert_bytes, local_data)
        checks.append(identity_match_check)
        if not identity_match_check.passed:
            return checks

        if "prefix" not in local_data:
            checks.append(
                ValidationCheck(
                    name="prefix_present",
                    passed=False,
                    message=(
                        "No 'prefix' field on this application's own databag; the requirer contract "
                        "requires it, and writing a canary without it would target an unscoped key."
                    ),
                )
            )
            return checks
        prefix = local_data["prefix"]

        try:
            credentials = grpc.ssl_channel_credentials(
                root_certificates=data["tls-ca"].encode(),
                private_key=key_bytes,
                certificate_chain=cert_bytes,
            )
        except (ValueError, grpc.RpcError) as exc:
            checks.append(
                ValidationCheck(
                    name="client_credentials", passed=False, message=f"Invalid client identity material: {exc}"
                )
            )
            return checks

        canary_key = f"{prefix}validator-canary-{uuid.uuid4().hex[:12]}"
        canary_value = f"validator-probe-{uuid.uuid4().hex[:12]}"

        with grpc.secure_channel(target, credentials) as channel:
            put_check = self._etcd_put(channel, canary_key, canary_value)
            checks.append(put_check)
            try:
                if put_check.passed:
                    checks.append(self._etcd_get_and_verify(channel, canary_key, canary_value))
            finally:
                # Always attempt cleanup, even if PUT reported failure: a client-side
                # timeout can still mean etcd committed the write server-side, which
                # would otherwise leave an orphaned canary key behind.
                checks.append(self._etcd_delete(channel, canary_key))
        return checks

    def _resolve_client_identity(self) -> tuple[str, str, ValidationCheck]:
        """Locate a client cert/key pair to authenticate as, per the module docstring convention."""
        cert_path = os.environ.get(ETCD_CLIENT_CERT_PATH_ENV, _DEFAULT_CLIENT_CERT_PATH)
        key_path = os.environ.get(ETCD_CLIENT_KEY_PATH_ENV, _DEFAULT_CLIENT_KEY_PATH)
        if not os.path.isfile(cert_path) or not os.path.isfile(key_path):
            return (
                cert_path,
                key_path,
                ValidationCheck(
                    name="client_identity",
                    passed=False,
                    message=(
                        f"No client cert/key found at '{cert_path}'/'{key_path}'. The etcd_client "
                        "interface never conveys a private key over the relation; provision one "
                        f"out-of-band (see {ETCD_CLIENT_KEY_PATH_ENV}) to run this check."
                    ),
                ),
            )
        return cert_path, key_path, ValidationCheck(name="client_identity", passed=True, message="OK")

    def _pick_grpc_target(self, uris: str) -> tuple[str, ValidationCheck]:
        """Derive a gRPC "host:port" target from the first published URI (stripping any scheme)."""
        first = uris.split(",")[0].strip()
        if not first:
            return "", ValidationCheck(name="uris_format", passed=False, message="uris field is empty.")
        try:
            parsed = urlsplit(first if "//" in first else f"//{first}")
            hostname, port = parsed.hostname, parsed.port
        except ValueError as exc:
            return "", ValidationCheck(
                name="uris_format", passed=False, message=f"Could not parse uri '{first}': {exc}"
            )
        if not hostname or not port:
            return "", ValidationCheck(name="uris_format", passed=False, message=f"Could not parse uri '{first}'.")
        # gRPC authorities require bracketed IPv6 literals (e.g. "[::1]:2379"), but
        # urlsplit().hostname strips the brackets, so restore them when the hostname
        # itself contains colons.
        authority_host = f"[{hostname}]" if ":" in hostname else hostname
        return f"{authority_host}:{port}", ValidationCheck(name="uris_format", passed=True, message="OK")

    def _local_databag(self) -> dict[str, str]:
        """Read this application's own contribution to the relation.

        Unlike ``self.databag`` (the remote application's data), the requirer's
        own ``prefix``/``mtls-cert`` fields live in this application's own
        databag on the relation, so they must be read directly from
        ``self.relation.data``.
        """
        return dict(self.relation.data[self.charm.app])

    def _resolve_local_mtls_cert(self, local_data: dict[str, str]) -> str | None:
        """Resolve this application's own published mtls-cert, secret-backed or plaintext."""
        if uri := local_data.get("secret-mtls"):
            return self.charm.model.get_secret(id=uri).get_content().get("mtls-cert")
        return local_data.get("mtls-cert")

    def _check_identity_matches_published_cert(
        self, loaded_cert_bytes: bytes, local_data: dict[str, str]
    ) -> ValidationCheck:
        """Verify the locally-provisioned client cert is the one actually published on this relation.

        Without this check, ``ETCD_CLIENT_CERT_PATH_ENV``/``ETCD_CLIENT_KEY_PATH_ENV`` could
        point at some other valid identity, and a PASS would validate that identity's ACLs
        rather than this relation's.
        """
        published_pem = self._resolve_local_mtls_cert(local_data)
        if not published_pem:
            return ValidationCheck(
                name="identity_match",
                passed=False,
                message="No mtls-cert published on this relation to compare against.",
            )
        loaded_check, loaded_cert = self._parse_mtls_cert_bytes(loaded_cert_bytes)
        if loaded_cert is None:
            return ValidationCheck(name="identity_match", passed=False, message=loaded_check.message)
        published_check, published_cert = self._parse_mtls_cert(published_pem)
        if published_cert is None:
            return ValidationCheck(name="identity_match", passed=False, message=published_check.message)
        if loaded_cert.fingerprint(hashes.SHA256()) != published_cert.fingerprint(hashes.SHA256()):
            return ValidationCheck(
                name="identity_match",
                passed=False,
                message="Locally-provisioned client cert does not match the mtls-cert published on this relation.",
            )
        return ValidationCheck(name="identity_match", passed=True, message="OK")

    def _etcd_put(self, channel: grpc.Channel, key: str, value: str) -> ValidationCheck:
        request = _encode_bytes_field(1, key.encode()) + _encode_bytes_field(2, value.encode())
        call = channel.unary_unary(
            f"/{_KV_SERVICE}/Put", request_serializer=lambda data: data, response_deserializer=lambda data: data
        )
        try:
            call(request, timeout=_GRPC_TIMEOUT_S)
        except grpc.RpcError as exc:
            return ValidationCheck(name="put", passed=False, message=f"PUT failed: {exc.details()}")
        return ValidationCheck(name="put", passed=True, message=f"Canary key '{key}' written.")

    def _etcd_get_and_verify(self, channel: grpc.Channel, key: str, expected_value: str) -> ValidationCheck:
        request = _encode_bytes_field(1, key.encode())
        call = channel.unary_unary(
            f"/{_KV_SERVICE}/Range", request_serializer=lambda data: data, response_deserializer=lambda data: data
        )
        try:
            response = call(request, timeout=_GRPC_TIMEOUT_S)
        except grpc.RpcError as exc:
            return ValidationCheck(name="get", passed=False, message=f"GET failed: {exc.details()}")

        try:
            fields = _decode_message(response)
            kvs = fields.get(2, [])  # RangeResponse.kvs (field 2), repeated KeyValue
            if not kvs:
                return ValidationCheck(name="get", passed=False, message=f"Canary key '{key}' not found after PUT.")
            kv_fields = _decode_message(kvs[0])  # type: ignore[arg-type]
            actual_bytes = kv_fields.get(5, [b""])[0]  # KeyValue.value (field 5)
            actual_value = actual_bytes.decode() if isinstance(actual_bytes, bytes) else ""
        except (IndexError, ValueError, TypeError, UnicodeDecodeError) as exc:
            return ValidationCheck(name="get", passed=False, message=f"Malformed GET response: {exc}")
        if actual_value != expected_value:
            return ValidationCheck(
                name="get",
                passed=False,
                message=f"Canary value mismatch: expected '{expected_value}', got '{actual_value}'.",
            )
        return ValidationCheck(name="get", passed=True, message="Canary value read back and verified.")

    def _etcd_delete(self, channel: grpc.Channel, key: str) -> ValidationCheck:
        request = _encode_bytes_field(1, key.encode())
        call = channel.unary_unary(
            f"/{_KV_SERVICE}/DeleteRange",
            request_serializer=lambda data: data,
            response_deserializer=lambda data: data,
        )
        try:
            call(request, timeout=_GRPC_TIMEOUT_S)
        except grpc.RpcError as exc:
            return ValidationCheck(name="delete", passed=False, message=f"DELETE failed: {exc.details()}")
        return ValidationCheck(name="delete", passed=True, message=f"Canary key '{key}' deleted.")

    # --- provides role: validating a submitted client cert from the provider's side ---

    def _validate_provides(self, level: ValidationLevel) -> ValidationResult:
        checks: list[ValidationCheck] = []

        error_result = self._check_relation_exists(level)
        if error_result:
            return error_result

        creds = self.resolve_secret("secret-mtls", "mtls-cert")
        schema_check = self.validate_schema(_PROVIDER_FIELDS, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level=level, checks=checks)

        data = self.databag | creds

        # Latency is timed from here, after secret/relation-data resolution, for the
        # same reason as the requires-side path: a slow Juju secret lookup for
        # secret-backed provider relations shouldn't count against the probe budget.
        start_time = time.monotonic()
        cert_check, cert = self._parse_mtls_cert(data["mtls-cert"])
        checks.append(cert_check)
        if not cert_check.passed:
            return self._make_result(level=level, checks=checks)

        checks.append(self._check_not_expired(cert))

        if level == "deep":
            # Verifying end-to-end read/write from the provider's side would require
            # internal, undocumented admin material not exposed via this interface
            # (etcd_client never gives the provider a private key either). That
            # coverage is exercised from the requires role instead; see the module
            # docstring. But an already-failed L1 check (e.g. an expired cert) must
            # still be reported as FAIL rather than discarded by SKIPPED, or the
            # runner's simple-level fallback would report an invalid relation as a
            # pass.
            if any(not check.passed for check in checks):
                return self._make_result(level=level, checks=checks)
            return self._skipped_result_due_to_level(level)

        elapsed = time.monotonic() - start_time
        checks.append(self._check_latency(elapsed, _SIMPLE_LATENCY_TARGET_S))
        return self._make_result(level=level, checks=checks)

    def _parse_mtls_cert(self, mtls_cert_pem: str) -> tuple[ValidationCheck, x509.Certificate | None]:
        """Parse the requirer's submitted client cert.

        The field may be a bundle of [client_cert, signing_ca] PEM blocks
        concatenated together; only the first (leaf) certificate is validated here.
        """
        return self._parse_mtls_cert_bytes(mtls_cert_pem.encode())

    def _parse_mtls_cert_bytes(self, mtls_cert_pem: bytes) -> tuple[ValidationCheck, x509.Certificate | None]:
        """Parse a submitted client cert given as raw bytes, without assuming they are UTF-8.

        Accepting bytes here (rather than requiring a decoded ``str``) lets callers
        report a malformed local cert file as a normal failed check instead of
        letting ``UnicodeDecodeError`` escape validation.
        """
        first_pem = mtls_cert_pem.split(b"-----END CERTIFICATE-----")[0] + b"-----END CERTIFICATE-----"
        try:
            cert = x509.load_pem_x509_certificate(first_pem)
        except ValueError as exc:
            return ValidationCheck(name="mtls_cert_parseable", passed=False, message=str(exc)), None
        return ValidationCheck(name="mtls_cert_parseable", passed=True, message="OK"), cert

    def _check_not_expired(self, cert: x509.Certificate | None, check_name: str = "validity_period") -> ValidationCheck:
        if cert is None:
            return ValidationCheck(name=check_name, passed=False, message="No certificate to check.")
        now = datetime.now(timezone.utc)
        if cert.not_valid_after_utc < now or cert.not_valid_before_utc > now:
            return ValidationCheck(name=check_name, passed=False, message="Certificate is not currently valid.")
        return ValidationCheck(name=check_name, passed=True, message="Certificate is within its validity period.")

    # --- shared helpers ---

    def _check_relation_exists(self, level: ValidationLevel) -> ValidationResult | None:
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        return None

    def _check_endpoints_format(self, endpoints: str) -> ValidationCheck:
        entries = [e.strip() for e in endpoints.split(",") if e.strip()]
        if not entries:
            return ValidationCheck(name="endpoints_format", passed=False, message="endpoints field is empty.")
        invalid = []
        for entry in entries:
            host, _, port_str = entry.rpartition(":")
            try:
                port_valid = bool(host) and 1 <= int(port_str) <= 65535
            except ValueError:
                port_valid = False
            if not port_valid:
                invalid.append(entry)
        if invalid:
            return ValidationCheck(
                name="endpoints_format", passed=False, message=f"Invalid endpoint entries: {', '.join(invalid)}"
            )
        return ValidationCheck(name="endpoints_format", passed=True, message=f"Validated {len(entries)} endpoint(s).")

    def _parse_tls_ca(self, tls_ca_pem: str) -> tuple[ValidationCheck, x509.Certificate | None]:
        try:
            cert = x509.load_pem_x509_certificate(tls_ca_pem.encode())
        except ValueError as exc:
            return ValidationCheck(name="tls_ca_pem", passed=False, message=str(exc)), None
        return ValidationCheck(name="tls_ca_pem", passed=True, message="OK"), cert

    def _check_latency(self, elapsed: float, target: float) -> ValidationCheck:
        if elapsed > target:
            return ValidationCheck(
                name="latency", passed=False, message=f"Validation took {elapsed:.2f}s, exceeded {target:.2f}s target."
            )
        return ValidationCheck(name="latency", passed=True, message=f"Validation completed in {elapsed:.2f}s.")
