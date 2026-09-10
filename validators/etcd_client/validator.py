# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from ipaddress import IPv6Address
from urllib.parse import urlsplit

import grpc
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID

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
# Best-effort cleanup after a PUT that already failed on this target doesn't need the full
# RPC timeout budget: the same unreachability that failed the PUT will fail DeleteRange too,
# so waiting the full _GRPC_TIMEOUT_S here would let one bad target consume most of the
# deep-level latency budget before the next advertised target is even tried.
_GRPC_CLEANUP_AFTER_FAILED_PUT_TIMEOUT_S = 1.0
# Bounds the *total* time _check_read_write may spend failing over across "uris" targets,
# independent of the per-target latency measurement reported to the caller: without this, a
# provider publishing many unreachable/stale entries could make deep validation block for
# len(targets) * (_GRPC_TIMEOUT_S + _GRPC_CLEANUP_AFTER_FAILED_PUT_TIMEOUT_S) seconds, since a
# fast final target still passes the (per-target-only) _DEEP_LATENCY_TARGET_S check regardless
# of how long earlier failovers took.
_MAX_TARGET_LOOP_DURATION_S = 30.0

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


_MAX_VARINT_BYTES = 10  # ceil(64 / 7): the longest a varint encoding a 64-bit value can be.


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a single protobuf varint starting at `pos`, bounding both length and reads.

    Server-provided bytes are untrusted input: without an explicit bound, a truncated or
    adversarial buffer whose continuation bit ("high bit") is set on every remaining byte
    would otherwise be read one byte past the buffer's end (`IndexError`, an implicit and
    unbounded-looking failure mode) or, for a buffer padded with high-bit-set bytes, loop for
    up to `len(data)` iterations shifting an ever-growing integer. Real protobuf varints never
    exceed 10 bytes (the encoding of a full 64-bit value), so both are rejected explicitly as
    a `ValueError` rather than relying on an eventual out-of-bounds access or unbounded CPU work.
    """
    result = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(data):
            raise ValueError(f"truncated varint starting at offset {start}")
        if pos - start >= _MAX_VARINT_BYTES:
            raise ValueError(f"overlong varint (>{_MAX_VARINT_BYTES} bytes) starting at offset {start}")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7


def _encode_bytes_field(field_number: int, value: bytes) -> bytes:
    tag = (field_number << 3) | 2  # wire type 2: length-delimited
    return _encode_varint(tag) + _encode_varint(len(value)) + value


def _encode_varint_field(field_number: int, value: int) -> bytes:
    tag = (field_number << 3) | 0  # wire type 0: varint
    return _encode_varint(tag) + _encode_varint(value)


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
            if pos + length > len(data):
                raise ValueError(f"truncated length-delimited field: declared length {length} at offset {pos}")
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

_REQUIRER_FIELDS = ["endpoints", "uris", "username", "tls-ca", "tls", "version"]
# "prefix" is checked separately from validate_schema() (see _validate_provides): an
# intentionally empty prefix (root of the keyspace) is a valid value, so its presence
# must be checked by key rather than truthiness, unlike "mtls-cert" which must be non-empty.
_PROVIDER_FIELDS = ["mtls-cert"]

# No valid hostname or IP literal contains whitespace or a C0/DEL control character (e.g. a
# literal NUL byte), but urlsplit() is lenient about both appearing inside parsed.hostname, so
# this is checked explicitly wherever a host is derived from "uris"/"endpoints" entries.
_INVALID_HOST_CHARS_RE = re.compile(r"[\s\x00-\x1f\x7f]")


def _redact_uri_for_message(uri: str) -> str:
    """Strip userinfo, query, and fragment components before including a URI in a diagnostic.

    Works purely textually (rather than via urlsplit) so it is safe to call even on a URI
    that fails to parse, and won't itself raise on malformed input. Userinfo is redacted
    whether or not a "scheme://" separator is present, since this interface's "uris" field
    also accepts bare "host:port" entries without a scheme.

    A genuine scheme is located first via a regex anchored to the *start* of the string: only
    a syntactically valid scheme (a letter followed by letters/digits/"+"/"."/"-") immediately
    followed by "://" counts, not merely the first "://" substring appearing anywhere. A
    malformed, scheme-less entry can otherwise contain userinfo followed by a "://" that isn't
    a real scheme delimiter at all (e.g. "admin:secret@https://host:2379" or
    "admin:secret://token@host:2379", where the "://" sits well past the start), and treating
    that as a trusted scheme boundary would leave everything before it -- including the
    credential -- unredacted. Only the text *after* a genuine scheme (or the whole string, if
    there is none) is then scanned for userinfo: unconditionally up to the *last* "@" in that
    remainder, regardless of any "/", "?", "#", or even embedded newline characters appearing
    before it (hence `re.DOTALL`, so "." can cross a literal newline rather than leaving text
    after it unredacted). This means malformed userinfo containing an embedded literal "@"
    (e.g. "admin@secret@host"), or containing what looks like a path/query/fragment delimiter
    before its own terminating "@" (e.g. "admin:secret?token@host" or
    "admin:secret?token/foo@host"), is always fully redacted rather than leaking a prefix of
    it. Query/fragment stripping is applied last, on whatever text remains (also with
    `re.DOTALL`, for the same embedded-newline reason), so it never has a chance to run before
    the userinfo redaction.
    """
    scheme_match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", uri)
    if scheme_match is None:
        prefix, rest = "", uri
    else:
        prefix, rest = scheme_match.group(0), uri[scheme_match.end() :]
    rest = re.sub(r".*@", "<redacted>@", rest, flags=re.DOTALL)
    sanitized = prefix + rest
    sanitized = re.sub(r"[?#].*$", "", sanitized, flags=re.DOTALL)
    # A malformed "uris" entry that fails parsing is still rejected, but its (redacted) text is
    # echoed back verbatim in the resulting ValidationCheck message; without this, an entry
    # containing a raw newline, carriage return, or other C0/DEL control character could forge
    # or split validator log output even though the uri itself never becomes a live target.
    # Escaping here (rather than only checking hostnames, as `_INVALID_HOST_CHARS_RE` does)
    # covers control characters anywhere in the string, not just within a successfully parsed
    # hostname.
    return re.sub(r"[\x00-\x1f\x7f]", lambda m: f"\\x{ord(m.group()):02x}", sanitized)


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

        tls_check = self._check_tls_enabled(data["tls"])
        checks.append(tls_check)
        if not tls_check.passed:
            return self._make_result(level=level, checks=checks)

        endpoints_check = self._check_endpoints_format(data["endpoints"])
        checks.append(endpoints_check)
        if not endpoints_check.passed:
            return self._make_result(level=level, checks=checks)

        tls_ca_check, ca_cert = self._parse_tls_ca(data["tls-ca"])
        checks.append(tls_ca_check)
        if not tls_ca_check.passed:
            return self._make_result(level=level, checks=checks)
        ca_validity_check = self._check_not_expired(ca_cert, check_name="tls_ca_validity_period")
        checks.append(ca_validity_check)
        if not ca_validity_check.passed:
            return self._make_result(level=level, checks=checks)

        # Validated at L1 too (not just before the L2 gRPC probe): "uris" is the
        # address(es) the deep probe connects to, so a malformed value must fail
        # simple-level validation rather than only surfacing once deep validation
        # is attempted.
        targets, target_check = self._pick_grpc_target(data["uris"])
        checks.append(target_check)
        if not target_check.passed:
            return self._make_result(level=level, checks=checks)

        # Latency is timed inside _check_tcp_reachable/_check_read_write themselves (from
        # only the final, winning target's own attempt), not wrapped around this call: a
        # prior unreachable target consuming its full connect/RPC timeout before a later
        # target succeeds must not be charged against the latency budget below, per the
        # multi-target failover policy documented on those methods. Juju secret/relation-data
        # resolution above -- which can be slow for cross-model relations independent of
        # etcd itself -- is likewise excluded, since timing starts only once probing begins.
        if level == "simple":
            # Derived from the same `targets` list already parsed from "uris" above (not
            # re-parsed from the separate "endpoints" field), so L1 reachability can never
            # diverge from what L2's gRPC probe actually connects to: a provider publishing
            # a reachable "endpoints" value alongside a stale or unreachable "uris" value
            # would otherwise let simple validation PASS despite deep being unable to
            # connect to any advertised gRPC target.
            check, elapsed = self._check_tcp_reachable(targets)
            checks.append(check)
        else:
            rw_checks, elapsed = self._check_read_write(data, targets)
            checks.extend(rw_checks)

        latency_target = _SIMPLE_LATENCY_TARGET_S if level == "simple" else _DEEP_LATENCY_TARGET_S
        checks.append(self._check_latency(elapsed, latency_target))

        return self._make_result(level=level, checks=checks)

    def _resolve_requirer_side_credentials(self) -> dict[str, str]:
        """Resolve provider-published fields, including secret-backed groups."""
        return {
            **self.resolve_secret("secret-user", "username", "uris"),
            **self.resolve_secret("secret-tls", "tls", "tls-ca"),
        }

    # Explicit allowlist rather than a denylist of "disabled" spellings: a denylist would
    # silently treat any typo or unrecognized value (e.g. "flase", "no", "0") as enabled, since
    # it isn't one of the specific rejected spellings.
    _TLS_ENABLED_VALUES = ("enabled", "true", "True")

    def _check_tls_enabled(self, tls: str) -> ValidationCheck:
        """Verify the provider actually advertises TLS as enabled on this relation.

        A missing, disabled, or unrecognized ``tls`` value would mean this validator's
        mTLS-only probe (and charmed-etcd's own TLS-only posture) is being run against a
        relation that never claimed to support it, so schema presence alone (a non-empty
        string) is not enough.
        """
        if tls.strip() not in self._TLS_ENABLED_VALUES:
            return ValidationCheck(
                name="tls_enabled",
                passed=False,
                message=(
                    f"Relation advertises tls='{tls}', which is not a recognized enabled value; "
                    "this interface is only usable over TLS."
                ),
            )
        return ValidationCheck(name="tls_enabled", passed=True, message="OK")

    def _check_tcp_reachable(self, targets: list[str]) -> tuple[ValidationCheck, float]:
        """Best-effort L1 reachability: open a raw TCP connection to an advertised target.

        ``targets`` are the same already-parsed "host:port" gRPC authorities (from "uris")
        that L2's read/write probe connects to (see ``_check_read_write``), so L1 and L2
        always agree on which addresses are being probed. Each is tried in turn until one
        succeeds, matching L2's own multi-target failover policy: "uris" enumerates cluster
        members, and a single unreachable member should not fail simple-level validation
        when another is reachable.

        A full mTLS handshake requires a client cert/key pair, which the interface
        never conveys (see module docstring), so this check is scoped to basic
        reachability rather than a completed TLS handshake.

        Returns the check alongside the elapsed time of only the *final* attempt (the one
        that produced the returned check), not the cumulative time across every attempt: a
        prior unreachable target can otherwise consume its full connect timeout before a
        later target succeeds quickly, and charging that failover time against the caller's
        latency budget would fail validation over a scenario the multi-target policy above is
        explicitly meant to tolerate.
        """
        last_check = ValidationCheck(name="connect", passed=False, message="No targets were provided to check.")
        attempt_start = time.monotonic()
        for target in targets:
            attempt_start = time.monotonic()
            # _pick_grpc_target() has already rejected any entry carrying userinfo (an "@"),
            # but redact defensively anyway: this message must never echo a credential verbatim.
            redacted_target = _redact_uri_for_message(target)
            host, _, port_str = target.rpartition(":")
            # socket.create_connection expects a bare IPv6 address (no brackets), while
            # gRPC targets use bracketed literals (e.g. "[::1]:2379") for disambiguation.
            host = host.removeprefix("[").removesuffix("]")
            try:
                with socket.create_connection((host, int(port_str)), timeout=_TCP_CONNECT_TIMEOUT_S):
                    return (
                        ValidationCheck(
                            name="connect", passed=True, message=f"TCP connection to '{redacted_target}' succeeded."
                        ),
                        time.monotonic() - attempt_start,
                    )
            except (OSError, ValueError) as exc:
                # socket.create_connection raises ValueError (not OSError) for some malformed
                # hosts, e.g. an embedded NUL byte; without catching it here, an
                # already-validated target could still crash validate() into an ERROR result
                # instead of a clean FAIL.
                last_check = ValidationCheck(
                    name="connect", passed=False, message=f"Could not reach '{redacted_target}': {exc}"
                )
        # targets is always non-empty here (see _pick_grpc_target), so the loop above always
        # runs at least once and overwrites this placeholder before it can be returned.
        return last_check, time.monotonic() - attempt_start

    def _check_read_write(self, data: dict[str, str], targets: list[str]) -> tuple[list[ValidationCheck], float]:
        """L2: mTLS PUT/GET/DELETE of a canary key using a locally-provisioned client identity.

        ``targets`` are the gRPC "host:port" authorities already parsed (and format-checked
        at L1) by the caller from ``data["uris"]``, in the order they were advertised. Each is
        tried in turn until one completes a PUT *and* GET *and* DELETE, since "uris" enumerates
        cluster members and a single unreachable/unhealthy member should not fail the canary
        when another is reachable; a target whose write succeeds but whose read or cleanup
        fails is treated the same as an unreachable one and failover continues.

        Returns the checks alongside an elapsed time that excludes time spent on any *prior,
        abandoned* target attempt, but not time spent on the *first* attempt's own setup: a
        prior unreachable target can otherwise consume most of its RPC timeout before a later
        target succeeds quickly, and charging that failover time against the caller's latency
        budget would fail validation over a scenario the multi-target policy above is
        explicitly meant to tolerate. The identity-resolution/cert-parsing work above this
        loop, however, is only ever done once, before the *first* attempt, so it is charged to
        that first attempt's elapsed time (and to every early-return path above the loop) by
        timing from function entry; only a *retry* after a failed-over target resets the timer
        to that retry's own start. Total time spent failing over across many targets is
        separately bounded by ``_MAX_TARGET_LOOP_DURATION_S``, independent of this
        per-attempt latency measurement.
        """
        func_start = time.monotonic()
        checks: list[ValidationCheck] = []

        cert_path, key_path, identity_check = self._resolve_client_identity()
        checks.append(identity_check)
        if not identity_check.passed:
            return checks, time.monotonic() - func_start

        try:
            with open(cert_path, "rb") as fh:
                cert_bytes = fh.read()
            with open(key_path, "rb") as fh:
                key_bytes = fh.read()
        except OSError as exc:
            checks.append(
                ValidationCheck(
                    name="client_identity_read", passed=False, message=f"Could not read client identity: {exc}"
                )
            )
            return checks, time.monotonic() - func_start

        # The requirer's own contribution to this relation (its "prefix" ACL grant
        # and its published mtls-cert) lives on this application's own databag, not
        # on the remote provider's databag (`data`, used above for endpoints/uris/
        # tls-ca): see BaseValidator.databag and _local_databag.
        local_data = self._local_databag()

        identity_match_check = self._check_identity_matches_published_cert(cert_bytes, local_data)
        checks.append(identity_match_check)
        if not identity_match_check.passed:
            return checks, time.monotonic() - func_start

        # The provider's "username" field is defined by the interface contract as the user
        # created from the client certificate's own common name, not an independent value:
        # without this check, a relation could publish username="alice" alongside a valid
        # cert for "bob" and still pass the mTLS canary authenticated as "bob".
        username_check = self._check_username_matches_cert_cn(cert_bytes, data["username"])
        checks.append(username_check)
        if not username_check.passed:
            return checks, time.monotonic() - func_start

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
            return checks, time.monotonic() - func_start
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
            return checks, time.monotonic() - func_start

        canary_key = f"{prefix}validator-canary-{uuid.uuid4().hex[:12]}"
        canary_value = f"validator-probe-{uuid.uuid4().hex[:12]}"

        # orphan_checks accumulates failed-cleanup checks from targets that were superseded by
        # a later, successful target: a client-side PUT failure can still mean etcd committed
        # the write server-side, so a subsequent DELETE failure on that same target must not be
        # silently dropped just because a *different* target's canary round-trip ultimately
        # succeeded (see test_preserves_cleanup_failure_from_superseded_target).
        orphan_checks: list[ValidationCheck] = []
        final_checks: list[ValidationCheck] = []
        # The first attempt is timed from func_start, not from just before its own PUT: the
        # identity-resolution/cert-parsing work already done above this loop is comparatively
        # small but not zero, and the common (single-target, no failover) case must still
        # attribute that time to the reported latency rather than silently dropping it. Only
        # retries after a failed-over target reset the timer to that attempt's own start, so a
        # slow/unreachable prior target still isn't charged against the caller's latency budget.
        target_start = func_start
        loop_deadline = time.monotonic() + _MAX_TARGET_LOOP_DURATION_S
        for i, target in enumerate(targets):
            if i > 0:
                if time.monotonic() >= loop_deadline:
                    checks.append(
                        ValidationCheck(
                            name="target_loop_deadline",
                            passed=False,
                            message=(
                                f"Gave up after {i} of {len(targets)} target(s): the overall "
                                f"{_MAX_TARGET_LOOP_DURATION_S:.0f}s target-loop deadline was exceeded "
                                "(failover across unreachable/stale targets cannot continue indefinitely)."
                            ),
                        )
                    )
                    break
                target_start = time.monotonic()
            iter_checks: list[ValidationCheck] = []
            get_check: ValidationCheck | None = None
            with grpc.secure_channel(target, credentials) as channel:
                put_check = self._etcd_put(channel, canary_key, canary_value)
                iter_checks.append(put_check)
                try:
                    if put_check.passed:
                        get_check = self._etcd_get_and_verify(channel, canary_key, canary_value)
                        iter_checks.append(get_check)
                finally:
                    # Always attempt cleanup, even if PUT reported failure: a client-side
                    # timeout can still mean etcd committed the write server-side, which
                    # would otherwise leave an orphaned canary key behind. A target whose PUT
                    # already failed gets a much shorter cleanup timeout: the same
                    # unreachability that failed the PUT will fail DeleteRange too, and this
                    # target is about to be abandoned in favor of the next one anyway.
                    cleanup_timeout = _GRPC_TIMEOUT_S if put_check.passed else _GRPC_CLEANUP_AFTER_FAILED_PUT_TIMEOUT_S
                    delete_check = self._etcd_delete(channel, canary_key, timeout=cleanup_timeout)
                    iter_checks.append(delete_check)
            final_checks = iter_checks
            # Only stop failover once PUT, GET (when attempted), and DELETE all passed: a
            # target whose write succeeds but whose Range RPC fails must not end the loop just
            # because its cleanup happened to succeed, since a later, still-untried target may
            # be fully healthy.
            if put_check.passed and get_check is not None and get_check.passed and delete_check.passed:
                break
            if not delete_check.passed and i < len(targets) - 1:
                reason = "after a failed PUT" if not put_check.passed else "after a successful PUT"
                orphan_checks.append(
                    ValidationCheck(
                        name="delete",
                        passed=False,
                        message=f"Cleanup failed for target '{target}' {reason}: {delete_check.message}",
                    )
                )
        checks.extend(orphan_checks)
        checks.extend(final_checks)
        return checks, time.monotonic() - target_start

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

    _SUPPORTED_URI_SCHEMES = ("", "https")

    def _pick_grpc_target(self, uris: str) -> tuple[list[str], ValidationCheck]:
        """Validate every comma-separated "uris" entry, deriving a gRPC target from each.

        All entries are format-checked (not just the first) so a malformed second/third
        endpoint fails validation rather than being silently ignored; the L2 probe tries
        each returned target in order (see ``_check_read_write``), since "uris" enumerates
        cluster members and a single unreachable member shouldn't fail the canary outright.
        """
        entries = [e.strip() for e in uris.split(",") if e.strip()]
        if not entries:
            return [], ValidationCheck(name="uris_format", passed=False, message="uris field is empty.")
        targets: list[str] = []
        for entry in entries:
            target, check = self._parse_single_uri(entry)
            if not check.passed:
                return [], check
            targets.append(target)
        return targets, ValidationCheck(name="uris_format", passed=True, message=f"Validated {len(entries)} uri(s).")

    def _parse_single_uri(self, entry: str) -> tuple[str, ValidationCheck]:
        """Parse and format-check a single "uris" entry, deriving a gRPC "host:port" target."""
        # Diagnostic messages below must never echo `entry` verbatim: a malformed uri can carry
        # userinfo (e.g. "user:password@host:2379") or a query/fragment (e.g. "?token=secret"),
        # either of which would otherwise leak a credential into validator output/logs. Sanitize
        # up front, working purely textually so this is safe even when urlsplit() itself fails.
        redacted_entry = _redact_uri_for_message(entry)
        if entry.startswith("//"):
            # A network-path reference (e.g. "//host:2379", scheme-relative but no scheme) is
            # neither of this interface's two documented forms (bare "host:port" or
            # "https://..."); urlsplit() would happily parse it with an empty scheme (which the
            # allowlist below also accepts, since a bare host:port also parses with an empty
            # scheme), so it must be rejected explicitly before that ambiguity can let it through.
            return "", ValidationCheck(
                name="uris_format",
                passed=False,
                message=(
                    f"uri '{redacted_entry}' is a network-path reference, which this interface "
                    "does not use; only a bare host:port or an 'https://' uri is accepted."
                ),
            )
        has_scheme = "://" in entry
        try:
            parsed = urlsplit(entry if has_scheme else f"//{entry}")
            hostname, port = parsed.hostname, parsed.port
        except ValueError as exc:
            # exc's own str() can itself embed the raw, unredacted offending substring (e.g.
            # Python's "Port could not be cast to integer value as 'hunter2'" when a malformed
            # uri's credential ends up parsed as the port, as with
            # "https://admin:hunter2?token@host:2379"), so it must never be included verbatim
            # in this message; only the exception *type* is reported, alongside the
            # already-redacted uri.
            return "", ValidationCheck(
                name="uris_format",
                passed=False,
                message=f"Could not parse uri '{redacted_entry}': {type(exc).__name__} while parsing.",
            )
        if not hostname or not port:
            return "", ValidationCheck(
                name="uris_format", passed=False, message=f"Could not parse uri '{redacted_entry}'."
            )
        if _INVALID_HOST_CHARS_RE.search(hostname):
            # urlsplit() is lenient about internal whitespace and control characters (e.g. a
            # literal NUL byte) in a hostname (e.g. "bad host" or "127.0.0.1\x00" both parse
            # successfully), but no valid hostname or IP literal ever contains either; a gRPC
            # channel target built from it would just fail to connect at runtime, so reject
            # it here as a format error instead.
            return "", ValidationCheck(
                name="uris_format",
                passed=False,
                message=f"uri '{redacted_entry}' has an invalid hostname containing whitespace or control characters.",
            )
        if parsed.username is not None or parsed.password is not None:
            return "", ValidationCheck(
                name="uris_format",
                passed=False,
                message=(
                    f"uri '{redacted_entry}' contains userinfo, which this interface does not use "
                    "(authentication is via mTLS and a separate 'username' field); rejecting it "
                    "rather than silently discarding it."
                ),
            )
        if parsed.path or parsed.query or parsed.fragment:
            return "", ValidationCheck(
                name="uris_format",
                passed=False,
                message=(
                    f"uri '{redacted_entry}' has a path/query/fragment component, which a bare "
                    "etcd client endpoint does not use; rejecting it rather than silently discarding it."
                ),
            )
        if has_scheme and parsed.scheme not in self._SUPPORTED_URI_SCHEMES:
            return "", ValidationCheck(
                name="uris_format",
                passed=False,
                message=(
                    f"uri '{redacted_entry}' uses scheme '{parsed.scheme}', which this mTLS-only "
                    "interface does not support; only a bare host:port or an 'https://' uri is accepted."
                ),
            )
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

        Mirrors ``BaseValidator.databag``'s defensive lookup: if this application hasn't
        published anything on the relation yet, ``self.charm.app`` may not be a key in
        ``self.relation.data`` at all, and indexing it directly would raise ``KeyError``
        instead of letting the caller's existing missing-field checks (e.g.
        ``prefix_present``) report a normal FAIL.
        """
        if self.charm.app not in self.relation.data:
            return {}
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

    def _check_username_matches_cert_cn(self, cert_bytes: bytes, expected_username: str) -> ValidationCheck:
        """Verify the client cert's leaf subject CN matches the provider's published "username".

        The interface contract defines "username" as derived from the client certificate's
        own common name, so this is a consistency check on the provider's own claim, not a
        cryptographic identity check (that's ``_check_identity_matches_published_cert``).
        """
        cert_check, cert = self._parse_mtls_cert_bytes(cert_bytes)
        if cert is None:
            return ValidationCheck(name="username_matches_cert_cn", passed=False, message=cert_check.message)
        cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        cn = str(cn_attrs[0].value) if cn_attrs else None
        if cn != expected_username:
            return ValidationCheck(
                name="username_matches_cert_cn",
                passed=False,
                message=f"Client cert common name '{cn}' does not match the published username '{expected_username}'.",
            )
        return ValidationCheck(name="username_matches_cert_cn", passed=True, message="OK")

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
            first_kv = kvs[0]
            if not isinstance(first_kv, bytes):
                # RangeResponse.kvs (field 2) is a length-delimited (wire type 2) repeated
                # field, which _decode_message always decodes as bytes; a malformed response
                # that instead encodes field 2 with wire type 0 (varint) would make kvs[0] an
                # int here, which _decode_message cannot itself further decode as a nested
                # message. Reporting that explicitly (rather than via a bare type: ignore
                # suppressing the type-checker's own correct concern) gives a clearer failure.
                return ValidationCheck(
                    name="get", passed=False, message="Malformed GET response: kvs[0] has the wrong wire type."
                )
            kv_fields = _decode_message(first_kv)
            actual_key_bytes = kv_fields.get(1, [b""])[0]  # KeyValue.key (field 1)
            actual_key = actual_key_bytes.decode() if isinstance(actual_key_bytes, bytes) else ""
            actual_bytes = kv_fields.get(5, [b""])[0]  # KeyValue.value (field 5)
            actual_value = actual_bytes.decode() if isinstance(actual_bytes, bytes) else ""
        except (IndexError, ValueError, TypeError, UnicodeDecodeError) as exc:
            return ValidationCheck(name="get", passed=False, message=f"Malformed GET response: {exc}")
        if actual_key != key:
            return ValidationCheck(
                name="get",
                passed=False,
                message=f"Canary key mismatch: expected '{key}', got '{actual_key}'.",
            )
        if actual_value != expected_value:
            return ValidationCheck(
                name="get",
                passed=False,
                # actual_value is attacker/environment-influenced data returned by the remote
                # etcd server (e.g. a malformed or unexpected response could return another
                # key's value entirely), so it must never be echoed verbatim into a diagnostic
                # message; only report that verification failed, not the mismatched content.
                message=f"Canary value mismatch: expected value not found for key '{key}'.",
            )
        return ValidationCheck(name="get", passed=True, message="Canary value read back and verified.")

    def _etcd_delete(self, channel: grpc.Channel, key: str, timeout: float = _GRPC_TIMEOUT_S) -> ValidationCheck:
        request = _encode_bytes_field(1, key.encode())
        call = channel.unary_unary(
            f"/{_KV_SERVICE}/DeleteRange",
            request_serializer=lambda data: data,
            response_deserializer=lambda data: data,
        )
        try:
            response = call(request, timeout=timeout)
        except grpc.RpcError as exc:
            return ValidationCheck(name="delete", passed=False, message=f"DELETE failed: {exc.details()}")
        try:
            fields = _decode_message(response)
            deleted = fields.get(2, [0])[0]  # DeleteRangeResponse.deleted (field 2)
        except (IndexError, ValueError, TypeError) as exc:
            return ValidationCheck(name="delete", passed=False, message=f"Malformed DELETE response: {exc}")
        # DeleteRangeResponse.deleted is a varint (int64) field. _decode_message decodes wire
        # type 0 (varint) as int and wire type 2 (length-delimited) as bytes, so a malformed
        # response that encodes field 2 with the wrong wire type would decode "deleted" as a
        # (possibly non-empty, truthy) bytes value instead. Require an actual positive int,
        # not just truthiness, so that case is reported as a failed cleanup rather than
        # silently accepted.
        if not isinstance(deleted, int) or deleted <= 0:
            # A successful gRPC status alone doesn't mean the canary was actually removed:
            # etcd reports the true outcome via DeleteRangeResponse.deleted, which is 0 for a
            # no-op deletion (e.g. the key was already gone, or the request targeted the wrong
            # key). Treating that as a passed cleanup would let validation succeed while the
            # canary is left behind.
            return ValidationCheck(name="delete", passed=False, message=f"DELETE reported no keys removed for '{key}'.")
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

        if "prefix" not in self.databag:
            checks.append(
                ValidationCheck(
                    name="prefix_present",
                    passed=False,
                    message="No 'prefix' field on the requirer's databag; the requirer contract requires it.",
                )
            )
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
            return self._make_result(
                status="SKIPPED",
                level=level,
                checks=checks,
                error=(
                    "Deep validation of the 'provides' role would require provider-side admin "
                    "material this interface never exposes (etcd_client gives the provider no "
                    "private key either); the read/write round-trip this would otherwise cover "
                    "is instead exercised from the 'requires' role (see module docstring)."
                ),
            )

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
            # Require an ASCII-decimal port string no longer than "65535" (5 digits): bare
            # int() accepts spellings like "1_000" or "+2379" that plain-english humans would
            # never write into an endpoint, silently normalizing them to a different numeric
            # port than what was actually configured. The length cap additionally guards
            # int()'s own global digit-count limit (Python's integer string conversion limit):
            # without it, an all-digit but excessively long port_str would raise ValueError
            # here uncaught, crashing validation into ERROR instead of a clean FAIL.
            port_valid = (
                bool(host)
                and port_str.isdigit()
                and port_str.isascii()
                and len(port_str) <= 5
                and 1 <= int(port_str) <= 65535
            )
            # rpartition(":") alone lets a malformed host through as long as the final segment
            # still parses as a port - e.g. "[::1:2379" (unbalanced brackets), the unbracketed
            # "::1:2379" (host "::1", which itself contains colons), and "[host:2379" (one-sided
            # bracket around a non-colon-bearing host) would all otherwise pass. This interface's
            # bracketed host:port literals (e.g. "[::1]:2379") are the only valid bracketed form,
            # so reject any one-sided bracket outright; require balanced brackets to actually
            # wrap an IPv6 literal (i.e. contain a colon) rather than a plain, non-IPv6 host like
            # "[10.1.2.3]" or an empty "[]"; and require balanced brackets whenever the host is
            # colon-bearing (which can only be a valid IPv6 literal bracketed).
            is_bracketed = host.startswith("[") and host.endswith("]")
            has_one_sided_bracket = host.startswith("[") != host.endswith("]")
            if has_one_sided_bracket:
                port_valid = False
            elif is_bracketed:
                # A colon alone doesn't prove the bracketed content is a valid IPv6 literal
                # (e.g. "[not-an-ipv6]:2379" contains no colon but "[not:an:ipv6]:2379" would
                # wrongly pass a bare colon check); parse it as an actual IPv6 address.
                try:
                    IPv6Address(host[1:-1])
                except ValueError:
                    port_valid = False
            elif ":" in host:
                port_valid = False
            # This interface's endpoints never carry userinfo (auth is via mTLS and a separate
            # "username" field, not a "user:pass@" prefix); reject it outright rather than
            # silently discarding it as part of the host.
            if "@" in host:
                port_valid = False
            if _INVALID_HOST_CHARS_RE.search(host):
                # No valid hostname or IP literal contains whitespace or a C0/DEL control
                # character (e.g. a literal NUL byte), but a naive rpartition(":")-based split
                # doesn't itself reject either (e.g. "bad host:2379" would otherwise pass);
                # mirrors the equivalent check in _parse_single_uri.
                port_valid = False
            if any(delimiter in host for delimiter in ("/", "?", "#")):
                # A naive rpartition(":")-based split doesn't reject a URI delimiter embedded
                # in the host, e.g. "host/path:2379" or "host?query:2379"; unlike
                # _parse_single_uri (which routes through urlsplit() and rejects any parsed
                # path/query/fragment separately), rpartition(":") here would otherwise treat
                # "host/path" as a plain (if unusual) hostname and accept the entry.
                port_valid = False
            if not port_valid:
                invalid.append(_redact_uri_for_message(entry))
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
