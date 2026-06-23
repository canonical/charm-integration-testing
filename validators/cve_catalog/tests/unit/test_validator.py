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

import urllib.error
import urllib.request
from typing import cast
from unittest.mock import MagicMock, patch

import ops

from validators.cve_catalog.validator import (
    CveCatalogValidator,
    _extract_entries,
    _validate_cve_id,
    _validate_entry_schema,
    _validate_total_entries,
    _validate_url_syntax,
)
from validators.test_utils.helpers import make_charm_from_relation, make_charm_from_relation_and_secrets
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_INTERFACE = "cve-catalog"
_ENDPOINT = "cve-catalog"


def _make_validator(
    databag: dict[str, str],
    endpoint: str = _ENDPOINT,
    role: RelationRoleStub = RelationRoleStub.requires,
    secrets: dict[str, dict[str, str]] | None = None,
) -> CveCatalogValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    if secrets:
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation_and_secrets(relation, secrets, role=role),
        )
    else:
        charm = cast(ops.CharmBase, make_charm_from_relation(relation, interface_name=_INTERFACE, role=role))
    return CveCatalogValidator(charm, cast(ops.Relation, relation))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "catalog-url": "http://cve-catalog.example.com/api/v1/cves",
    "total-entries": "42",
    "last-updated": "2026-01-15T12:00:00Z",
}

VALID_PAYLOAD: dict[str, list[dict[str, str]]] = {
    "entries": [
        {
            "cve_id": "CVE-2024-12345",
            "description": "A memory corruption vulnerability in libfoo.",
            "severity": "HIGH",
            "cvss_score": "8.8",
        }
    ]
}


# ---------------------------------------------------------------------------
# Unit-helper tests
# ---------------------------------------------------------------------------


class TestValidateUrlSyntax:
    def test_valid_http_url(self) -> None:
        check = _validate_url_syntax("http://example.com/api/cves")
        assert check.passed

    def test_valid_https_url(self) -> None:
        check = _validate_url_syntax("https://catalog.internal:8443/v1/cves")
        assert check.passed

    def test_invalid_scheme(self) -> None:
        check = _validate_url_syntax("ftp://catalog.example.com/cves")
        assert not check.passed
        assert "http" in check.message

    def test_missing_host(self) -> None:
        check = _validate_url_syntax("http:///path/only")
        assert not check.passed


class TestValidateTotalEntries:
    def test_valid_positive_integer(self) -> None:
        check = _validate_total_entries("100")
        assert check.passed

    def test_valid_zero(self) -> None:
        check = _validate_total_entries("0")
        assert check.passed

    def test_negative_integer_fails(self) -> None:
        check = _validate_total_entries("-1")
        assert not check.passed

    def test_non_numeric_fails(self) -> None:
        check = _validate_total_entries("not-a-number")
        assert not check.passed


class TestValidateEntrySchema:
    def test_valid_entry(self) -> None:
        entry = {"cve_id": "CVE-2024-1", "description": "desc", "severity": "HIGH"}
        check = _validate_entry_schema(entry)
        assert check.passed

    def test_missing_description_fails(self) -> None:
        entry = {"cve_id": "CVE-2024-1", "severity": "HIGH"}
        check = _validate_entry_schema(entry)
        assert not check.passed
        assert "description" in check.message

    def test_missing_multiple_fields(self) -> None:
        check = _validate_entry_schema({})
        assert not check.passed
        assert "cve_id" in check.message


class TestValidateCveId:
    def test_valid_cve_id(self) -> None:
        assert _validate_cve_id({"cve_id": "CVE-2024-12345"}).passed

    def test_case_insensitive(self) -> None:
        assert _validate_cve_id({"cve_id": "cve-2024-12345"}).passed

    def test_invalid_format_fails(self) -> None:
        check = _validate_cve_id({"cve_id": "CVE-12345"})
        assert not check.passed
        assert "CVE-YYYY-NNNNN" in check.message

    def test_empty_id_fails(self) -> None:
        assert not _validate_cve_id({}).passed


class TestExtractEntries:
    def test_list_payload(self) -> None:
        entries = _extract_entries([{"cve_id": "CVE-2024-1"}])
        assert len(entries) == 1

    def test_dict_with_entries_key(self) -> None:
        entries = _extract_entries({"entries": [{"cve_id": "CVE-2024-1"}]})
        assert len(entries) == 1

    def test_dict_with_vulnerabilities_key(self) -> None:
        entries = _extract_entries({"vulnerabilities": [{"cve_id": "CVE-2024-1"}]})
        assert len(entries) == 1

    def test_none_returns_empty(self) -> None:
        assert _extract_entries(None) == []

    def test_unknown_shape_returns_empty(self) -> None:
        assert _extract_entries({"unrecognised": "value"}) == []


# ---------------------------------------------------------------------------
# L1 – simple validation
# ---------------------------------------------------------------------------


class TestCveCatalogValidatorSimple:
    def test_skipped_for_unsupported_level(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        result = validator.validate(level="uat")
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_skipped_for_provider_role(self) -> None:
        validator = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides)
        result = validator.validate(level="simple")
        assert result.status == "SKIPPED"
        assert "provides" in (result.error or "")

    def test_error_when_no_remote_app(self) -> None:
        relation = RelationStub(name=_ENDPOINT, id=0, app=None, data={})
        charm = cast(ops.CharmBase, make_charm_from_relation(relation, interface_name=_INTERFACE))
        validator = CveCatalogValidator(charm, cast(ops.Relation, relation))
        result = validator.validate(level="simple")
        assert result.status == "ERROR"

    def test_fail_missing_catalog_url(self) -> None:
        databag = {"total-entries": "5"}
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "catalog-url" in schema_check.message

    def test_fail_missing_total_entries(self) -> None:
        databag = {"catalog-url": "http://example.com/cves"}
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "total-entries" in schema_check.message

    def test_fail_invalid_url_scheme(self) -> None:
        databag = {**VALID_DATABAG, "catalog-url": "ftp://bad.example.com/cves"}
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        url_check = next(c for c in result.checks if c.name == "url_syntax")
        assert not url_check.passed

    def test_fail_non_numeric_total_entries(self) -> None:
        databag = {**VALID_DATABAG, "total-entries": "bad"}
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        entries_check = next(c for c in result.checks if c.name == "total_entries")
        assert not entries_check.passed

    def test_pass_with_valid_databag_and_reachable_url(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers.get.return_value = "application/json"
        mock_resp.read.return_value = b'{"entries": []}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = validator.validate(level="simple")

        assert result.status == "PASS"
        reach_check = next(c for c in result.checks if c.name == "http_reachability")
        assert reach_check.passed

    def test_fail_when_url_unreachable(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            result = validator.validate(level="simple")

        assert result.status == "FAIL"
        reach_check = next(c for c in result.checks if c.name == "http_reachability")
        assert not reach_check.passed
        assert "connection refused" in reach_check.message

    def test_fail_when_http_error(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None),  # type: ignore[arg-type]
        ):
            result = validator.validate(level="simple")

        assert result.status == "FAIL"
        reach_check = next(c for c in result.checks if c.name == "http_reachability")
        assert not reach_check.passed

    def test_fail_when_non_json_content_type(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers.get.return_value = "text/html"
        mock_resp.read.return_value = b"<html>Not a JSON API</html>"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = validator.validate(level="simple")

        assert result.status == "FAIL"
        reach_check = next(c for c in result.checks if c.name == "http_reachability")
        assert not reach_check.passed

    def test_api_key_sent_in_authorization_header_via_secret(self) -> None:
        secret_id = "secret://model/secret-abc"
        databag = {**VALID_DATABAG, "secret-user": secret_id}
        secrets = {secret_id: {"api-key": "tok-SUPERSECRET"}}
        validator = _make_validator(databag, secrets=secrets)

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers.get.return_value = "application/json"
        mock_resp.read.return_value = b'{"entries": []}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        captured_req = {}

        def fake_urlopen(req: urllib.request.Request, timeout: float | None = None) -> MagicMock:
            captured_req["headers"] = dict(req.headers)
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = validator.validate(level="simple")

        assert result.status == "PASS"
        assert "Authorization" in captured_req["headers"]
        assert "tok-SUPERSECRET" in captured_req["headers"]["Authorization"]


# ---------------------------------------------------------------------------
# L2 – deep validation
# ---------------------------------------------------------------------------


class TestCveCatalogValidatorDeep:
    def _mock_urlopen(self, payload: dict[str, list[dict[str, str]]] | list[dict[str, str]]) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers.get.return_value = "application/json"
        mock_resp.read.return_value = (
            payload if isinstance(payload, bytes) else __import__("json").dumps(payload).encode()
        )
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_pass_with_valid_entry(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(VALID_PAYLOAD)):
            result = validator.validate(level="deep")
        assert result.status == "PASS", result.checks

    def test_fail_when_entry_missing_severity(self) -> None:
        payload = {"entries": [{"cve_id": "CVE-2024-9999", "description": "desc"}]}
        validator = _make_validator(VALID_DATABAG)
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(payload)):
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        entry_check = next(c for c in result.checks if c.name == "entry_schema")
        assert not entry_check.passed
        assert "severity" in entry_check.message

    def test_fail_when_cve_id_malformed(self) -> None:
        payload = {"entries": [{"cve_id": "VULN-12345", "description": "desc", "severity": "MEDIUM"}]}
        validator = _make_validator(VALID_DATABAG)
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(payload)):
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        id_check = next(c for c in result.checks if c.name == "cve_id_format")
        assert not id_check.passed

    def test_fail_when_catalog_empty(self) -> None:
        payload: dict[str, list[dict[str, str]]] = {"entries": []}
        validator = _make_validator(VALID_DATABAG)
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(payload)):
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        count_check = next(c for c in result.checks if c.name == "entry_count")
        assert not count_check.passed

    def test_fail_missing_required_fields(self) -> None:
        databag = {"catalog-url": "http://example.com/cves"}
        validator = _make_validator(databag)
        result = validator.validate(level="deep")
        assert result.status == "FAIL"
        assert any(c.name == "schema" and not c.passed for c in result.checks)

    def test_error_when_no_remote_app(self) -> None:
        relation = RelationStub(name=_ENDPOINT, id=0, app=None, data={})
        charm = cast(ops.CharmBase, make_charm_from_relation(relation, interface_name=_INTERFACE))
        validator = CveCatalogValidator(charm, cast(ops.Relation, relation))
        result = validator.validate(level="deep")
        assert result.status == "ERROR"

    def test_accepts_list_payload_shape(self) -> None:
        payload = [{"cve_id": "CVE-2025-00001", "description": "desc", "severity": "LOW"}]
        validator = _make_validator(VALID_DATABAG)
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(payload)):
            result = validator.validate(level="deep")
        assert result.status == "PASS", result.checks

    def test_skipped_for_provider_role(self) -> None:
        validator = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides)
        result = validator.validate(level="deep")
        assert result.status == "SKIPPED"
