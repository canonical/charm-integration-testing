# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import json
import socket
from typing import Any, Literal
from urllib.parse import urlparse

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_SCRAPE_METADATA_REQUIRED_KEYS = ("model", "model_uuid", "application", "unit")


class PrometheusScrapeValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)

        if self.role != "requires":
            return self._make_result(
                status="SKIPPED",
                level=level,
                error=f"Role '{self.role}' is not supported by {self.__class__.__name__}; only 'requires' is validated.",
            )

        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")

        databag = self.databag

        schema_check, scrape_jobs = _parse_databag(databag)
        checks: list[ValidationCheck] = [schema_check]
        if not schema_check.passed:
            return self._fail_result(level, checks)

        jobs_check = _validate_scrape_jobs(scrape_jobs)
        checks.append(jobs_check)
        if not jobs_check.passed:
            return self._fail_result(level, checks)

        if level == "deep":
            targets = _extract_targets(scrape_jobs)
            checks.append(_connectivity_check(targets))

        status: Literal["PASS", "FAIL"] = "PASS" if all(c.passed for c in checks) else "FAIL"
        return self._make_result(status, level, checks)


# ---------------------------------------------------------------------------
# Pure helpers
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
        if "static_configs" not in job:
            invalid.append(f"[{i}].static_configs missing")
            continue
        static_configs = job["static_configs"]
        if not isinstance(static_configs, list) or not static_configs:
            invalid.append(f"[{i}].static_configs must be a non-empty list")
            continue
        for j, sc in enumerate(static_configs):
            if not isinstance(sc, dict) or not sc.get("targets"):
                invalid.append(f"[{i}].static_configs[{j}].targets missing or empty")

    if invalid:
        return ValidationCheck(name="scrape_jobs", passed=False, message=f"Invalid scrape jobs: {'; '.join(invalid)}")
    return ValidationCheck(name="scrape_jobs", passed=True, message=f"OK ({len(scrape_jobs)} job(s))")


def _extract_targets(scrape_jobs: list[dict[str, Any]]) -> list[tuple[str, str, int]]:
    """Return a list of (target_str, host, port) tuples from all scrape jobs."""
    targets: list[tuple[str, str, int]] = []
    for job in scrape_jobs:
        scheme = job.get("scheme", "http")
        for sc in job.get("static_configs", []):
            for raw_target in sc.get("targets", []):
                try:
                    host, port = _parse_target(raw_target, scheme)
                    targets.append((raw_target, host, port))
                except Exception:  # nosec B110 - best-effort: skip unparseable targets
                    pass
    return targets


def _parse_target(target: str, scheme: str = "http") -> tuple[str, int]:
    """Parse a scrape target ``host:port`` or ``scheme://host:port`` into (host, port)."""
    if "://" not in target:
        target = f"{scheme}://{target}"
    parsed = urlparse(target)
    host = parsed.hostname or target
    if parsed.port is not None:
        return host, parsed.port
    return host, 443 if scheme in ("https",) else 80


def _connectivity_check(targets: list[tuple[str, str, int]]) -> ValidationCheck:
    """TCP-ping every scrape target; return a single pass/fail check."""
    if not targets:
        return ValidationCheck(name="connect", passed=False, message="No scrape targets found to test.")

    errors: list[str] = []
    for raw_target, host, port in targets:
        try:
            _tcp_ping(host, port)
        except Exception as exc:
            errors.append(f"{raw_target}: {exc}")

    if errors:
        return ValidationCheck(name="connect", passed=False, message="; ".join(errors))
    return ValidationCheck(name="connect", passed=True, message=f"Reached {len(targets)} target(s).")


def _tcp_ping(host: str, port: int, timeout: float = 5.0) -> None:
    """Open a TCP connection to host:port and immediately close it."""
    with socket.create_connection((host, port), timeout=timeout):
        pass
