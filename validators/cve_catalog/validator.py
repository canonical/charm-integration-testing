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
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
    ValidationResultStatus,
)

# Required top-level fields that the provider must populate in its databag.
_REQUIRED_FIELDS = ("catalog-url", "total-entries")

# Pattern for a canonical CVE identifier (e.g. CVE-2024-12345).
_CVE_ID_RE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)

# Required fields inside each individual CVE entry served by the catalog API.
_ENTRY_REQUIRED_FIELDS = ("cve_id", "description", "severity")

# HTTP request timeout in seconds.
_HTTP_TIMEOUT = 10


class CveCatalogValidator(BaseValidator):
    """Validator for the ``cve-catalog`` Juju interface.

    The provider side of this interface exposes a CVE catalog HTTP endpoint.
    The requirer is a passive consumer that reads vulnerability data from the
    provider's databag and uses ``catalog-url`` to fetch entries.

    Validation levels:
      * simple (L1): schema correctness, URL reachability, and JSON response.
      * deep   (L2): structural correctness of at least one CVE catalog entry.
    """

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level == "uat":
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        if level == "simple":
            return self._validate_simple()
        if level == "deep":
            return self._validate_deep()
        return self._skipped_result_due_to_level(level)

    # ------------------------------------------------------------------
    # L1 – schema + HTTP connectivity
    # ------------------------------------------------------------------

    def _validate_simple(self) -> ValidationResult:
        """L1: Validate required fields and confirm the catalog URL is reachable."""
        checks: list[ValidationCheck] = []

        # 1. Resolve optional API key from Juju secret or plain databag field.
        creds = self.resolve_secret("secret-user", "api-key")

        # 2. Schema: catalog-url and total-entries must be present.
        schema_check = self.validate_schema(list(_REQUIRED_FIELDS), creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("simple", checks)

        # 3. Validate the URL is syntactically correct.
        url = self.databag["catalog-url"]
        url_check = _validate_url_syntax(url)
        checks.append(url_check)
        if not url_check.passed:
            return self._build_result("simple", checks)

        # 4. Validate total-entries is a non-negative integer.
        entries_check = _validate_total_entries(self.databag["total-entries"])
        checks.append(entries_check)
        if not entries_check.passed:
            return self._build_result("simple", checks)

        # 5. HTTP reachability: expect 200 with application/json body.
        api_key = creds.get("api-key")
        reach_check, _ = _fetch_catalog(url, api_key, limit=1)
        checks.append(reach_check)

        return self._build_result("simple", checks)

    # ------------------------------------------------------------------
    # L2 – canary entry validation
    # ------------------------------------------------------------------

    def _validate_deep(self) -> ValidationResult:
        """L2: Validate that the catalog exposes well-formed CVE entries."""
        checks: list[ValidationCheck] = []

        # 1. Resolve optional API key.
        creds = self.resolve_secret("secret-user", "api-key")

        # 2. Schema check.
        schema_check = self.validate_schema(list(_REQUIRED_FIELDS), creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("deep", checks)

        # 3. URL syntax check.
        url = self.databag["catalog-url"]
        url_check = _validate_url_syntax(url)
        checks.append(url_check)
        if not url_check.passed:
            return self._build_result("deep", checks)

        # 4. total-entries check.
        entries_check = _validate_total_entries(self.databag["total-entries"])
        checks.append(entries_check)
        if not entries_check.passed:
            return self._build_result("deep", checks)

        # 5. Fetch catalog (request at least one entry).
        api_key = creds.get("api-key")
        reach_check, payload = _fetch_catalog(url, api_key, limit=1)
        checks.append(reach_check)
        if not reach_check.passed:
            return self._build_result("deep", checks)

        # 6. Verify the payload contains at least one entry.
        entries = _extract_entries(payload)
        count_check = ValidationCheck(
            name="entry_count",
            passed=len(entries) > 0,
            message=f"Got {len(entries)} entry/entries." if entries else "Catalog returned no CVE entries.",
        )
        checks.append(count_check)
        if not count_check.passed:
            return self._build_result("deep", checks)

        # 7. Validate required fields and CVE-ID format on the first entry.
        checks.append(_validate_entry_schema(entries[0]))
        checks.append(_validate_cve_id(entries[0]))

        return self._build_result("deep", checks)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_result(self, level: ValidationLevel, checks: list[ValidationCheck]) -> ValidationResult:
        status: ValidationResultStatus = "PASS" if all(c.passed for c in checks) else "FAIL"
        return self._make_result(status=status, level=level, checks=checks)


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _validate_url_syntax(url: str) -> ValidationCheck:
    """Return a check confirming *url* is a well-formed http/https URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Scheme '{parsed.scheme}' is not http/https or host is missing.")
    except ValueError as exc:
        return ValidationCheck(
            name="url_syntax",
            passed=False,
            message=f"Invalid catalog-url '{url}': {exc}. Expected a well-formed http(s):// URL.",
        )
    return ValidationCheck(name="url_syntax", passed=True, message=f"catalog-url '{url}' is a valid URL.")


def _validate_total_entries(raw: str) -> ValidationCheck:
    """Return a check confirming *raw* represents a non-negative integer."""
    try:
        count = int(raw)
        if count < 0:
            raise ValueError("Value must be >= 0.")
    except (ValueError, TypeError) as exc:
        return ValidationCheck(
            name="total_entries",
            passed=False,
            message=f"total-entries '{raw}' is not a valid non-negative integer: {exc}.",
        )
    return ValidationCheck(name="total_entries", passed=True, message=f"total-entries is {count}.")


def _fetch_catalog(
    url: str, api_key: str | None, limit: int
) -> tuple[ValidationCheck, dict[str, Any] | list[Any] | None]:
    """Perform an HTTP GET against *url* and return a (check, parsed-body) pair.

    When *limit* is greater than 0 it is appended as a ``?limit=`` query parameter
    to bound the response size. When *limit* is 0 no limit parameter is sent and
    the full catalog body is fetched.
    Any ``api-key`` value is sent as an ``Authorization: Bearer`` header.
    Returns ``(check, None)`` when the request fails.
    """
    parts = urllib.parse.urlsplit(url)
    if limit > 0:
        existing = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        existing["limit"] = [str(limit)]
        new_query = urllib.parse.urlencode(existing, doseq=True)
        parts = parts._replace(query=new_query)
    full_url = urllib.parse.urlunsplit(parts)

    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = urllib.request.Request(full_url, headers=headers)
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310  # nosec B310
            status_code = resp.status
            content_type = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return (
            ValidationCheck(
                name="http_reachability",
                passed=False,
                message=(
                    f"HTTP {exc.code} from catalog URL '{url}'. "
                    f"Verify the endpoint is running and the URL in 'catalog-url' is correct."
                ),
            ),
            None,
        )
    except urllib.error.URLError as exc:
        return (
            ValidationCheck(
                name="http_reachability",
                passed=False,
                message=(
                    f"Cannot reach catalog URL '{url}': {exc.reason}. "
                    f"Check that the service is running and the host/port are accessible."
                ),
            ),
            None,
        )
    except Exception as exc:
        return (
            ValidationCheck(
                name="http_reachability",
                passed=False,
                message=f"Unexpected error fetching '{url}': {exc}.",
            ),
            None,
        )

    if status_code != 200:
        return (
            ValidationCheck(
                name="http_reachability",
                passed=False,
                message=(
                    f"Expected HTTP 200 from '{url}', got {status_code}. " f"Verify the catalog endpoint is healthy."
                ),
            ),
            None,
        )

    if "application/json" not in content_type:
        return (
            ValidationCheck(
                name="http_reachability",
                passed=False,
                message=(
                    f"Expected Content-Type 'application/json' from '{url}', "
                    f"got '{content_type}'. The endpoint may not be a CVE catalog API."
                ),
            ),
            None,
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return (
            ValidationCheck(
                name="http_reachability",
                passed=False,
                message=f"Response from '{url}' is not valid JSON: {exc}.",
            ),
            None,
        )

    return (
        ValidationCheck(
            name="http_reachability",
            passed=True,
            message=f"Catalog URL '{url}' returned HTTP 200 with valid JSON.",
        ),
        payload,
    )


def _extract_entries(payload: dict[str, Any] | list[Any] | None) -> list[dict[str, Any]]:
    """Extract a list of CVE entry dicts from a catalog API response payload.

    Accepts two common response shapes:
    * A JSON array:  ``[{...}, {...}]``
    * A JSON object with an ``entries`` or ``vulnerabilities`` list key.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if isinstance(payload, dict):
        for key in ("entries", "vulnerabilities", "cves", "data", "items"):
            if key in payload and isinstance(payload[key], list):
                return [e for e in payload[key] if isinstance(e, dict)]
    return []


def _validate_entry_schema(entry: dict[str, Any]) -> ValidationCheck:
    """Return a check confirming the required fields are present in *entry*."""
    missing = [f for f in _ENTRY_REQUIRED_FIELDS if not entry.get(f)]
    if missing:
        return ValidationCheck(
            name="entry_schema",
            passed=False,
            message=(
                f"CVE entry is missing required fields: {', '.join(missing)}. "
                f"Each entry must contain: {', '.join(_ENTRY_REQUIRED_FIELDS)}."
            ),
        )
    return ValidationCheck(name="entry_schema", passed=True, message="CVE entry has all required fields.")


def _validate_cve_id(entry: dict[str, Any]) -> ValidationCheck:
    """Return a check confirming the ``cve_id`` field follows CVE-YYYY-NNNNN format."""
    cve_id = str(entry.get("cve_id", ""))
    if not _CVE_ID_RE.match(cve_id):
        return ValidationCheck(
            name="cve_id_format",
            passed=False,
            message=(
                f"cve_id '{cve_id}' does not match the expected CVE-YYYY-NNNNN format "
                f"(e.g. CVE-2024-12345). Ensure the catalog uses standard CVE identifiers."
            ),
        )
    return ValidationCheck(
        name="cve_id_format",
        passed=True,
        message=f"cve_id '{cve_id}' matches CVE-YYYY-NNNNN format.",
    )
