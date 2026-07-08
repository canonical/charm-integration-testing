# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import re
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
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
# Hosts that are bind-all placeholders and cannot be used as scrape targets directly.
_WILDCARD_HOSTS: frozenset[str] = frozenset({"*", "0.0.0.0"})  # nosec B104
# Regex to detect a bare IPv6 address (not already bracketed).
_IPV6_RE = re.compile(r"^[0-9a-fA-F:]+:[0-9a-fA-F:]*$")


@dataclass
class _ScrapeTarget:
    """A single resolved scrape target extracted from a scrape job."""

    raw: str
    scheme: str
    host: str
    port: int
    metrics_path: str
    labels: dict[str, str] = field(default_factory=dict)


class PrometheusScrapeValidator(BaseValidator):
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
            if (addr := dict(self.relation.data[unit]).get("prometheus_scrape_unit_address", ""))
        )
        targets, parse_errors = _extract_targets(scrape_jobs, unit_addresses=unit_addresses)

        # Report any target parsing errors
        if parse_errors:
            parse_check = ValidationCheck(
                name="target_parsing",
                passed=False,
                message=f"Failed to parse {len(parse_errors)} target(s): {'; '.join(parse_errors[:3])}"
                + (f" (and {len(parse_errors) - 3} more)" if len(parse_errors) > 3 else ""),
            )
            checks.append(parse_check)
            if not targets:
                # If we have no valid targets at all, fail early
                return self._fail_result(level, checks)

        # L1: HTTP GET the metrics endpoint on the first target; verify 200 OK.
        http_check = _http_probe_check(targets[:1])
        checks.append(http_check)
        if not http_check.passed:
            return self._fail_result(level, checks)

        if level == "deep":
            # L2: scrape all targets, parse Prometheus text exposition format.
            checks.extend(_scrape_and_parse_checks(targets))
            # L2: verify static labels are valid Prometheus label name/value pairs.
            checks.append(_static_labels_check(scrape_jobs))

        return self._make_result(level=level, checks=checks)


# ---------------------------------------------------------------------------
# Pure helpers — databag parsing
# ---------------------------------------------------------------------------


def _parse_databag(databag: dict[str, str]) -> tuple[ValidationCheck, list[dict[str, Any]]]:
    """Validate the databag schema and return (check, scrape_jobs).

    If the check did not pass, scrape_jobs will be an empty list.
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
    """Verify scrape_jobs is a non-empty list with valid structure."""
    if not isinstance(scrape_jobs, list) or not scrape_jobs:
        return ValidationCheck(name="scrape_jobs", passed=False, message="'scrape_jobs' must be a non-empty list.")

    invalid: list[str] = []
    for i, job in enumerate(scrape_jobs):
        if not isinstance(job, dict):
            invalid.append(f"[{i}] is not an object")
            continue

        # Validate scheme (only http/https allowed)
        scheme = job.get("scheme", "http")
        if not isinstance(scheme, str) or scheme not in ("http", "https"):
            invalid.append(f"[{i}].scheme must be 'http' or 'https', got {scheme!r}")

        # Validate metrics_path (must start with /)
        metrics_path = job.get("metrics_path", "/metrics")
        if not isinstance(metrics_path, str) or not metrics_path.startswith("/"):
            invalid.append(f"[{i}].metrics_path must be a string starting with '/', got {metrics_path!r}")

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
# Pure helpers — target extraction
# ---------------------------------------------------------------------------


def _host_for_url(host: str) -> str:
    """Wrap bare IPv6 addresses in square brackets for use in URLs.

    ``urlparse`` stores the hostname without brackets (e.g. ``2001:db8::1``),
    but RFC 3986 requires brackets when the address appears in a URL authority
    component (``http://[2001:db8::1]:9104/``).  IPv4 addresses and hostnames
    are returned unchanged.
    """
    return f"[{host}]" if _IPV6_RE.match(host) else host


def _extract_targets(
    scrape_jobs: list[dict[str, Any]], unit_addresses: list[str] | None = None
) -> tuple[list[_ScrapeTarget], list[str]]:
    """Return deduplicated scrape targets from all jobs and any parse errors.

    Deduplication is based on the fully resolved scrape URL (scheme+host+port+metrics_path).
    This ensures targets with different schemes or metrics paths are treated as distinct.

    When a target uses a wildcard bind-all host (``*`` or ``0.0.0.0``), it is expanded
    into one concrete target per entry in *unit_addresses*, using the per-unit
    ``prometheus_scrape_unit_address`` values from the relation databag.

    Returns:
        tuple: (targets, parse_errors) where parse_errors is a list of error messages.
    """
    targets: list[_ScrapeTarget] = []
    parse_errors: list[str] = []
    seen: set[str] = set()  # Set of fully resolved URLs for deduplication
    for job in scrape_jobs:
        job_scheme = job.get("scheme", "http")
        metrics_path = job.get("metrics_path", "/metrics")
        for sc in job.get("static_configs", []):
            sc_labels: dict[str, str] = sc.get("labels") or {}
            if not isinstance(sc_labels, dict):
                sc_labels = {}
            for raw_target in sc.get("targets", []):
                try:
                    # Parse target to get effective scheme, host, and port
                    effective_scheme, host, port = _parse_target(raw_target, job_scheme)

                    # Expand wildcard bind-all hosts to concrete unit addresses
                    if host in _WILDCARD_HOSTS:
                        resolved_hosts = unit_addresses or []
                        if not resolved_hosts:
                            parse_errors.append(
                                f"'{raw_target}': wildcard host requires unit address data"
                                " (no prometheus_scrape_unit_address in relation databag)"
                            )
                            continue
                    else:
                        resolved_hosts = [host]

                    for resolved_host in resolved_hosts:
                        # Deduplicate on the full scrape URL
                        scrape_url = f"{effective_scheme}://{_host_for_url(resolved_host)}:{port}{metrics_path}"
                        if scrape_url in seen:
                            continue
                        seen.add(scrape_url)

                        targets.append(
                            _ScrapeTarget(
                                raw=raw_target,
                                scheme=effective_scheme,
                                host=resolved_host,
                                port=port,
                                metrics_path=metrics_path,
                                labels=dict(sc_labels),
                            )
                        )
                except Exception as exc:
                    parse_errors.append(f"'{raw_target}': {exc}")
    return targets, parse_errors


def _parse_target(target: str, scheme: str = "http") -> tuple[str, str, int]:
    """Parse a scrape target into (scheme, host, port).

    Target can be:
    - host:port (uses provided scheme)
    - scheme://host:port (uses target's scheme)

    Returns:
        tuple: (effective_scheme, host, port)
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
# Pure helpers — L1: HTTP probe
# ---------------------------------------------------------------------------


def _http_probe_check(targets: list[_ScrapeTarget]) -> ValidationCheck:
    """HTTP GET the metrics endpoint on the given targets; verify 200 OK response."""
    if not targets:
        return ValidationCheck(name="http_probe", passed=False, message="No parseable scrape targets found to probe.")

    errors: list[str] = []
    for t in targets:
        url = f"{t.scheme}://{_host_for_url(t.host)}:{t.port}{t.metrics_path}"
        try:
            with urlopen(url, timeout=5) as resp:  # nosec B310
                if resp.status != 200:
                    errors.append(f"{url}: HTTP {resp.status}")
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    if errors:
        return ValidationCheck(name="http_probe", passed=False, message="; ".join(errors))
    return ValidationCheck(
        name="http_probe", passed=True, message=f"HTTP 200 OK from {targets[0].host}:{targets[0].port}."
    )


# ---------------------------------------------------------------------------
# Pure helpers — L2: scrape and parse
# ---------------------------------------------------------------------------


def _scrape_and_parse_checks(targets: list[_ScrapeTarget]) -> list[ValidationCheck]:
    """HTTP GET each target's metrics endpoint and parse Prometheus text exposition format.

    Returns one check per target; each check passes only when the response is 200 OK and
    contains at least one metric family.
    """
    checks: list[ValidationCheck] = []
    for t in targets:
        target_id = f"{t.host}:{t.port}"
        url = f"{t.scheme}://{_host_for_url(t.host)}:{t.port}{t.metrics_path}"
        check_name = f"scrape[{target_id}]"
        try:
            with urlopen(url, timeout=10) as resp:  # nosec B310
                body = resp.read().decode("utf-8", errors="replace")
            if resp.status != 200:
                checks.append(
                    ValidationCheck(name=check_name, passed=False, message=f"HTTP {resp.status} from {target_id}.")
                )
                continue
            has_metrics, family_count = _parse_prometheus_text(body)
            if has_metrics:
                checks.append(
                    ValidationCheck(
                        name=check_name,
                        passed=True,
                        message=f"Scraped {family_count} metric family(ies) from {target_id}.",
                    )
                )
            else:
                checks.append(
                    ValidationCheck(
                        name=check_name,
                        passed=False,
                        message=f"No metric families found in response from {target_id}.",
                    )
                )
        except Exception as exc:
            checks.append(ValidationCheck(name=check_name, passed=False, message=f"Scrape failed: {exc}"))
    return checks


def _parse_prometheus_text(text: str) -> tuple[bool, int]:
    """Parse Prometheus text exposition format; return (has_metrics, family_count).

    A metric family is identified by a ``# HELP`` line.  When no ``# HELP``
    lines are found (minimal exporters that omit them), non-comment, non-empty
    lines are counted instead so that bare metric values are not rejected.
    """
    family_count = sum(1 for line in text.splitlines() if line.startswith("# HELP "))
    if family_count == 0:
        family_count = sum(1 for line in text.splitlines() if line and not line.startswith("#"))
    return family_count > 0, family_count


def _static_labels_check(scrape_jobs: list[dict[str, Any]]) -> ValidationCheck:
    """Verify that static labels in scrape jobs have valid Prometheus label names and string values."""
    errors: list[str] = []
    for i, job in enumerate(scrape_jobs):
        for j, sc in enumerate(job.get("static_configs", [])):
            labels = sc.get("labels")
            if labels is None:
                continue
            if not isinstance(labels, dict):
                errors.append(f"job[{i}].static_configs[{j}].labels must be a JSON object")
                continue
            for k, v in labels.items():
                if not isinstance(k, str) or not _LABEL_NAME_RE.match(k):
                    errors.append(f"job[{i}].static_configs[{j}]: invalid label name {k!r}")
                if not isinstance(v, str):
                    errors.append(f"job[{i}].static_configs[{j}]: value for label {k!r} must be a string")

    if errors:
        return ValidationCheck(name="labels", passed=False, message="; ".join(errors))
    return ValidationCheck(name="labels", passed=True, message="Static labels are valid.")
