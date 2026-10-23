# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import ipaddress
import json
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_SCRAPE_METADATA_REQUIRED_KEYS = ("model", "model_uuid", "application", "unit")
# Per-unit relation-data key advertising a provider unit's profiling address.
_UNIT_ADDRESS_KEY = "parca_scrape_unit_address"
# Hosts that are bind-all placeholders and cannot be used as scrape targets directly.
_WILDCARD_HOSTS: frozenset[str] = frozenset({"*", "0.0.0.0"})  # nosec B104
# Conventional Go net/http/pprof index served by profiling providers.
_PROFILING_PROBE_PATH = "/debug/pprof/"
# TCP connect timeout in seconds.
_TCP_TIMEOUT = 5.0
# HTTP profiling-probe timeout in seconds.
_HTTP_TIMEOUT = 10.0


@dataclass
class _ScrapeTarget:
    """A single resolved profiling scrape target extracted from a scrape job."""

    raw: str
    scheme: str
    host: str
    port: int
    labels: dict[str, str] = field(default_factory=dict)


class ParcaScrapeValidator(BaseValidator):
    """Validator for the ``parca_scrape`` Juju interface.

    The provider side exposes one or more profiling endpoints (pprof) that the
    Parca charm (the requirer/consumer) scrapes. The provider advertises its
    scrape configuration through the ``scrape_metadata`` and ``scrape_jobs``
    application-databag keys, and each provider unit advertises its address via
    the ``parca_scrape_unit_address`` unit-databag key.

    Validation levels:
      * simple (L1): schema correctness, scrape-job structure, target
        resolution, and TCP reachability of every resolved target.
      * deep   (L2): everything in simple plus an HTTP probe of each target's
        pprof endpoint to confirm the profiling server responds.
    """

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)

        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        schema_check, scrape_jobs = _parse_databag(self.databag)
        checks: list[ValidationCheck] = [schema_check]
        if not schema_check.passed:
            return self._fail_result(level, checks)

        jobs_check = _validate_scrape_jobs(scrape_jobs)
        checks.append(jobs_check)
        if not jobs_check.passed:
            return self._fail_result(level, checks)

        unit_addresses = sorted(
            addr
            for unit in self.relation.units
            if (addr := self.relation.data[unit].get(_UNIT_ADDRESS_KEY, "").strip())
        )
        targets, parse_errors = _extract_targets(scrape_jobs, unit_addresses=unit_addresses)

        if parse_errors:
            checks.append(
                ValidationCheck(
                    name="target_parsing",
                    passed=False,
                    message=f"Failed to parse {len(parse_errors)} target(s): {'; '.join(parse_errors[:3])}"
                    + (f" (and {len(parse_errors) - 3} more)" if len(parse_errors) > 3 else ""),
                )
            )
            if not targets:
                return self._fail_result(level, checks)

        tcp_check = _tcp_reachability_check(targets)
        checks.append(tcp_check)
        if not tcp_check.passed:
            return self._fail_result(level, checks)

        if level == "deep":
            checks.extend(_http_profiling_checks(targets))

        return self._make_result(level=level, checks=checks)


# ---------------------------------------------------------------------------
# Pure helpers -- databag parsing
# ---------------------------------------------------------------------------


def _parse_databag(databag: dict[str, str]) -> tuple[ValidationCheck, list[dict[str, Any]]]:
    """Validate the databag schema and return ``(check, scrape_jobs)``.

    If the check did not pass, ``scrape_jobs`` will be an empty list.
    """
    schema_check = _validate_schema(databag)
    if not schema_check.passed:
        return schema_check, []

    try:
        scrape_jobs = json.loads(databag["scrape_jobs"])
    except json.JSONDecodeError as exc:
        return (
            ValidationCheck(name="schema", passed=False, message=f"'scrape_jobs' is not valid JSON: {exc}"),
            [],
        )

    return schema_check, scrape_jobs


def _validate_schema(databag: dict[str, str]) -> ValidationCheck:
    """Check presence and validity of required databag fields."""
    missing_top = [f for f in ("scrape_metadata", "scrape_jobs") if not databag.get(f)]
    if missing_top:
        return ValidationCheck(
            name="schema",
            passed=False,
            message=f"Missing required fields: {', '.join(missing_top)}",
        )

    try:
        metadata = json.loads(databag["scrape_metadata"])
    except json.JSONDecodeError as exc:
        return ValidationCheck(name="schema", passed=False, message=f"'scrape_metadata' is not valid JSON: {exc}")

    if not isinstance(metadata, dict):
        return ValidationCheck(
            name="schema",
            passed=False,
            message=f"'scrape_metadata' must be a JSON object, got {type(metadata).__name__}",
        )

    missing_meta = [k for k in _SCRAPE_METADATA_REQUIRED_KEYS if not metadata.get(k)]
    if missing_meta:
        return ValidationCheck(
            name="schema",
            passed=False,
            message=f"'scrape_metadata' missing keys: {', '.join(missing_meta)}",
        )

    return ValidationCheck(name="schema", passed=True, message="OK")


def _validate_scrape_jobs(scrape_jobs: list[dict[str, Any]]) -> ValidationCheck:
    """Verify ``scrape_jobs`` is a non-empty list with valid structure."""
    if not isinstance(scrape_jobs, list) or not scrape_jobs:
        return ValidationCheck(name="scrape_jobs", passed=False, message="'scrape_jobs' must be a non-empty list.")

    invalid: list[str] = []
    for i, job in enumerate(scrape_jobs):
        if not isinstance(job, dict):
            invalid.append(f"[{i}] is not an object")
            continue

        scheme = job.get("scheme", "http")
        if not isinstance(scheme, str) or scheme not in ("http", "https"):
            invalid.append(f"[{i}].scheme must be 'http' or 'https', got {scheme!r}")

        if "static_configs" not in job:
            invalid.append(f"[{i}].static_configs missing")
            continue
        static_configs = job["static_configs"]
        if not isinstance(static_configs, list) or not static_configs:
            invalid.append(f"[{i}].static_configs must be a non-empty list")
            continue
        for j, sc in enumerate(static_configs):
            if not isinstance(sc, dict):
                invalid.append(f"[{i}].static_configs[{j}] must be a dict")
                continue
            targets = sc.get("targets")
            if not isinstance(targets, list) or not targets:
                invalid.append(f"[{i}].static_configs[{j}].targets must be a non-empty list")
                continue
            if not all(isinstance(t, str) and t for t in targets):
                invalid.append(f"[{i}].static_configs[{j}].targets must contain only non-empty strings")

    if invalid:
        return ValidationCheck(name="scrape_jobs", passed=False, message=f"Invalid scrape jobs: {'; '.join(invalid)}")
    return ValidationCheck(name="scrape_jobs", passed=True, message=f"OK ({len(scrape_jobs)} job(s))")


# ---------------------------------------------------------------------------
# Pure helpers -- target extraction
# ---------------------------------------------------------------------------


def _host_for_url(host: str) -> str:
    """Wrap bare IPv6 addresses in square brackets for use in URLs.

    ``urlparse`` stores the hostname without brackets (e.g. ``2001:db8::1``),
    but RFC 3986 requires brackets when the address appears in a URL authority
    component. IPv4 addresses and hostnames are returned unchanged.
    """
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            return f"[{host}]"
    except ValueError:
        pass
    return host


def _extract_targets(
    scrape_jobs: list[dict[str, Any]], unit_addresses: list[str] | None = None
) -> tuple[list[_ScrapeTarget], list[str]]:
    """Return deduplicated profiling targets from all jobs and any parse errors.

    Deduplication is based on the resolved ``scheme://host:port`` triple. When a
    target uses a wildcard bind-all host (``*`` or ``0.0.0.0``), it is expanded
    into one concrete target per entry in *unit_addresses*, taken from the
    ``parca_scrape_unit_address`` values in the relation databag.
    """
    targets: list[_ScrapeTarget] = []
    parse_errors: list[str] = []
    seen: set[str] = set()
    for job in scrape_jobs:
        job_scheme = job.get("scheme", "http")
        for sc in job.get("static_configs", []):
            sc_labels = sc.get("labels") or {}
            if not isinstance(sc_labels, dict):
                sc_labels = {}
            for raw_target in sc.get("targets", []):
                try:
                    effective_scheme, host, port = _parse_target(raw_target, job_scheme)

                    if host in _WILDCARD_HOSTS:
                        resolved_hosts = unit_addresses or []
                        if not resolved_hosts:
                            parse_errors.append(
                                f"'{raw_target}': wildcard host requires unit address data"
                                f" (no {_UNIT_ADDRESS_KEY} in relation databag)"
                            )
                            continue
                    else:
                        resolved_hosts = [host]

                    for resolved_host in resolved_hosts:
                        key = f"{effective_scheme}://{_host_for_url(resolved_host)}:{port}"
                        if key in seen:
                            continue
                        seen.add(key)
                        targets.append(
                            _ScrapeTarget(
                                raw=raw_target,
                                scheme=effective_scheme,
                                host=resolved_host,
                                port=port,
                                labels=dict(sc_labels),
                            )
                        )
                except Exception as exc:
                    parse_errors.append(f"'{raw_target}': {exc}")
    return targets, parse_errors


def _parse_target(target: str, scheme: str = "http") -> tuple[str, str, int]:
    """Parse a scrape target into ``(scheme, host, port)``.

    Target can be ``host:port`` (uses the provided *scheme*) or
    ``scheme://host:port`` (uses the target's own scheme).
    """
    if "://" not in target:
        target = f"{scheme}://{target}"
    parsed = urlparse(target)
    host = parsed.hostname or target
    effective_scheme = parsed.scheme or scheme
    if parsed.port is not None:
        return effective_scheme, host, parsed.port
    return effective_scheme, host, 443 if effective_scheme == "https" else 80


# ---------------------------------------------------------------------------
# Pure helpers -- L1: TCP reachability
# ---------------------------------------------------------------------------


def _tcp_reachability_check(targets: list[_ScrapeTarget]) -> ValidationCheck:
    """Open and immediately close a TCP connection to every target."""
    if not targets:
        return ValidationCheck(name="connect", passed=False, message="No parseable profiling targets found to probe.")

    errors: list[str] = []
    for t in targets:
        target_id = f"{_host_for_url(t.host)}:{t.port}"
        try:
            with socket.create_connection((t.host, t.port), timeout=_TCP_TIMEOUT):
                pass
        except OSError as exc:
            errors.append(f"{target_id}: {exc}")

    if errors:
        return ValidationCheck(name="connect", passed=False, message="; ".join(errors))
    return ValidationCheck(
        name="connect",
        passed=True,
        message=f"TCP connection succeeded to {len(targets)} target(s).",
    )


# ---------------------------------------------------------------------------
# Pure helpers -- L2: HTTP profiling probe
# ---------------------------------------------------------------------------


def _http_profiling_checks(targets: list[_ScrapeTarget]) -> list[ValidationCheck]:
    """HTTP GET each target's pprof index and confirm the server responds.

    Returns one check per target. A profiling endpoint is considered healthy
    when it responds with any HTTP status below 500; individual pprof profile
    paths may legitimately return 404, but a 5xx (or no response at all)
    indicates the profiling server is unavailable.
    """
    checks: list[ValidationCheck] = []
    for t in targets:
        target_id = f"{_host_for_url(t.host)}:{t.port}"
        url = f"{t.scheme}://{target_id}{_PROFILING_PROBE_PATH}"
        check_name = f"profiling[{target_id}]"
        try:
            with urlopen(url, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
                status = resp.status
        except Exception as exc:  # noqa: BLE001 - urlopen raises a broad set of errors
            status = _http_status_from_error(exc)
            if status is None:
                checks.append(ValidationCheck(name=check_name, passed=False, message=f"Profiling probe failed: {exc}"))
                continue

        if status < 500:
            checks.append(
                ValidationCheck(
                    name=check_name,
                    passed=True,
                    message=f"Profiling endpoint at {target_id} responded (HTTP {status}).",
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    name=check_name,
                    passed=False,
                    message=f"Profiling endpoint at {target_id} returned HTTP {status}.",
                )
            )
    return checks


def _http_status_from_error(exc: Exception) -> int | None:
    """Extract an HTTP status code from a ``urlopen`` error, if present.

    ``HTTPError`` is a valid response carrying a status code (e.g. 404), which
    we treat as proof the server responded. Other errors (connection refused,
    timeout) have no status and signal an unreachable endpoint.
    """
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None
