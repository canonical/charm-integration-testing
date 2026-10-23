# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from typing import cast
from unittest.mock import MagicMock, patch

import ops

from validators.parca_scrape.validator import ParcaScrapeValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
    UnitStub,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

VALID_SCRAPE_METADATA = json.dumps(
    {
        "model": "test-model",
        "model_uuid": "abc-123",
        "application": "my-app",
        "unit": "my-app/0",
    }
)

VALID_SCRAPE_JOBS = json.dumps(
    [
        {
            "static_configs": [{"targets": ["my-app-0.my-app.svc.cluster.local:8080"]}],
            "scheme": "http",
        }
    ]
)

VALID_DATABAG: dict[str, str] = {
    "scrape_metadata": VALID_SCRAPE_METADATA,
    "scrape_jobs": VALID_SCRAPE_JOBS,
}

WILDCARD_SCRAPE_JOBS = json.dumps([{"static_configs": [{"targets": ["*:8080"]}]}])


def _make_validator(
    databag: dict[str, str],
    endpoint: str = "profiling-endpoint",
    role: RelationRoleStub = RelationRoleStub.requires,
    unit_addresses: dict[str, str] | None = None,
) -> ParcaScrapeValidator:
    app = ApplicationStub()
    data: dict[ApplicationStub | UnitStub | None, dict[str, str]] = {app: databag}
    units: set[UnitStub] = set()
    for name, addr in (unit_addresses or {}).items():
        unit = UnitStub(name)
        data[unit] = {"parca_scrape_unit_address": addr}
        units.add(unit)
    relation = RelationStub(name=endpoint, id=0, app=app, data=data, units=frozenset(units))
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(relation, role=role, interface_name="parca_scrape"),
    )
    return ParcaScrapeValidator(charm, cast(ops.Relation, relation))


# ---------------------------------------------------------------------------
# Level / role gating
# ---------------------------------------------------------------------------


class TestGating:
    def test_returns_skipped_for_unsupported_level(self) -> None:
        result = _make_validator(VALID_DATABAG).validate(level="uat")
        assert result.status == "SKIPPED"

    def test_returns_skipped_for_provides_role(self) -> None:
        result = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides).validate()
        assert result.status == "SKIPPED"

    def test_returns_error_when_no_remote_app(self) -> None:
        relation = RelationStub(name="profiling-endpoint", id=0, app=None, data={})
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="parca_scrape"),
        )
        result = ParcaScrapeValidator(charm, cast(ops.Relation, relation)).validate()
        assert result.status == "ERROR"


# ---------------------------------------------------------------------------
# Schema / structure
# ---------------------------------------------------------------------------


class TestSchema:
    def test_missing_fields_fail(self) -> None:
        result = _make_validator({"scrape_metadata": VALID_SCRAPE_METADATA}).validate()
        assert result.status == "FAIL"
        assert result.checks[0].name == "schema"
        assert not result.checks[0].passed

    def test_invalid_metadata_keys_fail(self) -> None:
        databag = {
            "scrape_metadata": json.dumps({"model": "m"}),
            "scrape_jobs": VALID_SCRAPE_JOBS,
        }
        result = _make_validator(databag).validate()
        assert result.status == "FAIL"
        assert result.checks[0].name == "schema"

    def test_invalid_json_scrape_jobs_fail(self) -> None:
        databag = {"scrape_metadata": VALID_SCRAPE_METADATA, "scrape_jobs": "not-json"}
        result = _make_validator(databag).validate()
        assert result.status == "FAIL"

    def test_empty_scrape_jobs_fail(self) -> None:
        databag = {"scrape_metadata": VALID_SCRAPE_METADATA, "scrape_jobs": "[]"}
        result = _make_validator(databag).validate()
        assert result.status == "FAIL"
        assert result.checks[-1].name == "scrape_jobs"

    def test_wildcard_without_unit_addresses_fails_target_parsing(self) -> None:
        databag = {"scrape_metadata": VALID_SCRAPE_METADATA, "scrape_jobs": WILDCARD_SCRAPE_JOBS}
        result = _make_validator(databag).validate()
        assert result.status == "FAIL"
        assert any(c.name == "target_parsing" for c in result.checks)


# ---------------------------------------------------------------------------
# Simple level -- TCP reachability
# ---------------------------------------------------------------------------


class TestSimple:
    @patch("validators.parca_scrape.validator.socket.create_connection")
    def test_happy_path_pass(self, mock_conn: MagicMock) -> None:
        result = _make_validator(VALID_DATABAG).validate(level="simple")
        assert result.status == "PASS"
        assert {c.name for c in result.checks} == {"schema", "scrape_jobs", "connect"}
        mock_conn.assert_called_once()

    @patch("validators.parca_scrape.validator.socket.create_connection", side_effect=OSError("refused"))
    def test_tcp_failure_fails(self, _mock_conn: MagicMock) -> None:
        result = _make_validator(VALID_DATABAG).validate(level="simple")
        assert result.status == "FAIL"
        assert result.checks[-1].name == "connect"
        assert not result.checks[-1].passed

    @patch("validators.parca_scrape.validator.socket.create_connection")
    def test_wildcard_expands_to_unit_addresses(self, mock_conn: MagicMock) -> None:
        databag = {"scrape_metadata": VALID_SCRAPE_METADATA, "scrape_jobs": WILDCARD_SCRAPE_JOBS}
        result = _make_validator(
            databag,
            unit_addresses={"my-app/0": "10.0.0.1", "my-app/1": "10.0.0.2"},
        ).validate(level="simple")
        assert result.status == "PASS"
        assert mock_conn.call_count == 2


# ---------------------------------------------------------------------------
# Deep level -- HTTP profiling probe
# ---------------------------------------------------------------------------


def _mock_http_response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.status = status
    return resp


class TestDeep:
    @patch("validators.parca_scrape.validator.urlopen")
    @patch("validators.parca_scrape.validator.socket.create_connection")
    def test_deep_happy_path_pass(self, _mock_conn: MagicMock, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_http_response(200)
        result = _make_validator(VALID_DATABAG).validate(level="deep")
        assert result.status == "PASS"
        assert any(c.name.startswith("profiling[") for c in result.checks)

    @patch("validators.parca_scrape.validator.urlopen")
    @patch("validators.parca_scrape.validator.socket.create_connection")
    def test_deep_404_still_passes(self, _mock_conn: MagicMock, mock_urlopen: MagicMock) -> None:
        from urllib.error import HTTPError

        mock_urlopen.side_effect = HTTPError("http://x/", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        result = _make_validator(VALID_DATABAG).validate(level="deep")
        assert result.status == "PASS"

    @patch("validators.parca_scrape.validator.urlopen", side_effect=OSError("refused"))
    @patch("validators.parca_scrape.validator.socket.create_connection")
    def test_deep_connection_error_fails(self, _mock_conn: MagicMock, _mock_urlopen: MagicMock) -> None:
        result = _make_validator(VALID_DATABAG).validate(level="deep")
        assert result.status == "FAIL"
        assert any(c.name.startswith("profiling[") and not c.passed for c in result.checks)

    @patch("validators.parca_scrape.validator.urlopen")
    @patch("validators.parca_scrape.validator.socket.create_connection")
    def test_deep_5xx_fails(self, _mock_conn: MagicMock, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_http_response(503)
        result = _make_validator(VALID_DATABAG).validate(level="deep")
        assert result.status == "FAIL"
