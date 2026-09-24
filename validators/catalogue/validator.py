# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
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

# Required fields the consumer must publish in its own application databag.
_REQUIRED_FIELDS = ("name", "url", "icon")

# HTTP request timeout in seconds.
_HTTP_TIMEOUT = 10

# Path at which the catalogue provider serves its aggregated catalogue.
_CONFIG_PATH = "/config.json"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())


class CatalogueValidator(BaseValidator):
    """Validator for the ``catalogue`` Juju interface.

    The requirer (consumer) publishes a catalogue item (``name``, ``url``,
    ``icon`` and optional ``description``/``api_docs``/``api_endpoints``) into
    its own application databag on the relation. The provider (catalogue charm)
    aggregates every consumer's item and serves them as JSON over HTTP.

    Validation levels:
      * simple (L1): the consumer's own databag contains a well-formed item.
      * deep   (L2): the provider's HTTP endpoint serves the consumer's item.
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
    # L1 – local databag schema
    # ------------------------------------------------------------------

    def _validate_simple(self) -> ValidationResult:
        """L1: Validate the item this application published on the relation."""
        checks: list[ValidationCheck] = []
        local = self._local_databag()

        schema_check = self.validate_schema(list(_REQUIRED_FIELDS), data=local)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("simple", checks)

        url_check = _validate_url_syntax(local["url"])
        checks.append(url_check)
        if not url_check.passed:
            return self._build_result("simple", checks)

        checks.append(_validate_api_endpoints(local.get("api_endpoints", "")))

        return self._build_result("simple", checks)

    # ------------------------------------------------------------------
    # L2 – provider serves the item
    # ------------------------------------------------------------------

    def _validate_deep(self) -> ValidationResult:
        """L2: Confirm the provider's HTTP endpoint serves this application's item."""
        checks: list[ValidationCheck] = []
        local = self._local_databag()

        schema_check = self.validate_schema(list(_REQUIRED_FIELDS), data=local)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("deep", checks)

        url_check = _validate_url_syntax(local["url"])
        checks.append(url_check)
        if not url_check.passed:
            return self._build_result("deep", checks)

        endpoints_check = _validate_api_endpoints(local.get("api_endpoints", ""))
        checks.append(endpoints_check)
        if not endpoints_check.passed:
            return self._build_result("deep", checks)

        fetch_check, payload = _fetch_catalogue(self._provider_config_urls())
        checks.append(fetch_check)
        if not fetch_check.passed:
            return self._build_result("deep", checks)

        checks.append(_validate_item_served(payload, local))

        return self._build_result("deep", checks)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _local_databag(self) -> dict[str, str]:
        """Read this application's own contribution to the relation.

        Unlike ``self.databag`` (the remote application's data), the consumer's
        ``name``/``url``/``icon`` fields live in this application's own databag
        on the relation, so they must be read directly from ``self.relation.data``.

        Mirrors ``BaseValidator.databag``'s defensive lookup: if this application
        hasn't published anything on the relation yet, ``self.charm.app`` may not
        be a key in ``self.relation.data`` at all, and indexing it directly would
        raise ``KeyError`` instead of letting the schema check report a normal FAIL.
        """
        if self.charm.app not in self.relation.data:
            return {}
        return dict(self.relation.data[self.charm.app])

    def _provider_config_urls(self) -> list[str]:
        """Derive HTTP and HTTPS URLs at which the provider may serve its catalogue."""
        provider = self.relation.app.name if self.relation.app else ""
        model = self.charm.model.name
        host = f"{provider}.{model}.svc.cluster.local"
        return [f"http://{host}{_CONFIG_PATH}", f"https://{host}{_CONFIG_PATH}"]

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
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(character.isspace() for character in url)
        ):
            raise ValueError(f"Scheme '{parsed.scheme}' is not http/https or host is missing.")
        parsed.port
    except ValueError as exc:
        return ValidationCheck(
            name="url_syntax",
            passed=False,
            message=f"Catalogue URL is invalid: {exc}. Expected a well-formed http(s):// URL.",
        )
    return ValidationCheck(name="url_syntax", passed=True, message="Catalogue URL is a valid HTTP(S) URL.")


def _validate_api_endpoints(raw: str) -> ValidationCheck:
    """Return a check confirming *raw* is a JSON object mapping names to URLs.

    ``api_endpoints`` is optional; an empty value is accepted.
    """
    if not raw or raw == "null":
        return ValidationCheck(name="api_endpoints", passed=True, message="api_endpoints is not set (optional).")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return ValidationCheck(
            name="api_endpoints",
            passed=False,
            message=f"api_endpoints is not valid JSON: {exc}.",
        )
    if not isinstance(parsed, dict):
        return ValidationCheck(
            name="api_endpoints",
            passed=False,
            message=f"api_endpoints must be a JSON object, got {type(parsed).__name__}.",
        )
    return ValidationCheck(
        name="api_endpoints",
        passed=True,
        message=f"api_endpoints is a JSON object with {len(parsed)} entry/entries.",
    )


def _fetch_catalogue(urls: list[str]) -> tuple[ValidationCheck, dict[str, Any] | None]:
    """Perform an HTTP GET against *url* and return a (check, parsed-body) pair.

    Returns ``(check, None)`` when the request fails or the body is not a JSON object.
    """
    errors: list[str] = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with _HTTP_OPENER.open(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
                status_code = resp.status
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            errors.append(f"HTTP {exc.code}")
            continue
        except urllib.error.URLError as exc:
            errors.append(str(exc.reason))
            continue
        except OSError as exc:
            errors.append(str(exc))
            continue

        if status_code != 200:
            errors.append(f"HTTP {status_code}")
            continue
        break
    else:
        return (
            ValidationCheck(
                name="http_reachability",
                passed=False,
                message=f"Cannot reach catalogue provider over HTTP or HTTPS: {'; '.join(errors)}.",
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
                message=f"Catalogue provider response is not valid JSON: {exc}.",
            ),
            None,
        )

    if not isinstance(payload, dict):
        return (
            ValidationCheck(
                name="http_reachability",
                passed=False,
                message=f"Catalogue provider response is not a JSON object, got {type(payload).__name__}.",
            ),
            None,
        )

    return (
        ValidationCheck(
            name="http_reachability",
            passed=True,
            message="Catalogue provider returned HTTP 200 with valid JSON.",
        ),
        payload,
    )


def _validate_item_served(payload: dict[str, Any] | None, expected: dict[str, str]) -> ValidationCheck:
    """Return a check confirming the expected item is present in the served catalogue."""
    apps = payload.get("apps") if isinstance(payload, dict) else None
    if not isinstance(apps, list):
        return ValidationCheck(
            name="item_served",
            passed=False,
            message="Served catalogue has no 'apps' list.",
        )
    name = expected["name"]
    for item in apps:
        if not isinstance(item, dict) or item.get("name") != name:
            continue
        fields_match = all(
            _catalogue_field_matches(field, item.get(field), expected[field]) for field in _REQUIRED_FIELDS
        )
        fields_match = fields_match and all(
            item.get(field) == value
            for field, value in expected.items()
            if field not in _REQUIRED_FIELDS and field != "api_endpoints" and field in item
        )
        if "api_endpoints" in expected and "api_endpoints" in item:
            try:
                fields_match = fields_match and item.get("api_endpoints") == json.loads(expected["api_endpoints"])
            except json.JSONDecodeError:
                fields_match = False
        if fields_match:
            return ValidationCheck(
                name="item_served",
                passed=True,
                message=f"Item '{name}' is present in the served catalogue.",
            )

    served = [item.get("name") for item in apps if isinstance(item, dict)]
    if name not in served:
        return ValidationCheck(
            name="item_served",
            passed=False,
            message=(
                f"Item '{name}' is not present in the served catalogue. "
                f"Served items: {', '.join(str(s) for s in served) or '(none)'}."
            ),
        )
    return ValidationCheck(
        name="item_served",
        passed=False,
        message=f"Item '{name}' is present but does not match the published catalogue fields.",
    )


def _catalogue_field_matches(field: str, actual: Any, expected: str) -> bool:
    if field != "url":
        return bool(actual == expected)
    if not isinstance(actual, str):
        return False
    try:
        actual_url = urllib.parse.urlparse(actual)
        expected_url = urllib.parse.urlparse(expected)
    except ValueError:
        return False
    if not _validate_url_syntax(actual_url.geturl()).passed:
        return False
    if not _validate_url_syntax(expected_url.geturl()).passed:
        return False
    return (
        actual_url.scheme == expected_url.scheme
        and actual_url.port == expected_url.port
        and actual_url.username == expected_url.username
        and actual_url.password == expected_url.password
        and actual_url.path == expected_url.path
        and actual_url.params == expected_url.params
        and actual_url.query == expected_url.query
        and actual_url.fragment == expected_url.fragment
    )
