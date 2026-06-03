# Copyright (C) 2026 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import json
import socket
from typing import cast
from unittest.mock import patch

import ops

from validators.prometheus_scrape.validator import PrometheusScrapeValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_validator(databag: dict[str, str], endpoint: str = "metrics-endpoint") -> PrometheusScrapeValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    charm = cast(ops.CharmBase, make_charm_from_relation(relation, interface_name="prometheus_scrape"))
    return PrometheusScrapeValidator(charm, cast(ops.Relation, relation))


VALID_SCRAPE_METADATA = json.dumps(
    {
        "model": "test-model",
        "model_uuid": "abc-123",
        "application": "my-app",
        "unit": "my-app/0",
        "charm_name": "my-charm",
    }
)

VALID_SCRAPE_JOBS = json.dumps(
    [
        {
            "metrics_path": "/metrics",
            "static_configs": [{"targets": ["my-app-0.my-app.svc.cluster.local:8080"]}],
            "scheme": "http",
        }
    ]
)

VALID_DATABAG: dict[str, str] = {
    "scrape_metadata": VALID_SCRAPE_METADATA,
    "scrape_jobs": VALID_SCRAPE_JOBS,
}


# ---------------------------------------------------------------------------
# Tests: simple level
# ---------------------------------------------------------------------------


class TestPrometheusScrapeValidatorSimple:
    def test_returns_skipped_for_unsupported_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_returns_error_when_no_remote_app(self) -> None:
        # GIVEN a relation whose app is not present in its data dict
        app = ApplicationStub()
        relation = RelationStub(name="metrics-endpoint", id=0, app=app, data={app: {}})
        # Replace relation.app with a different stub so relation_exists() returns False.
        relation.app = ApplicationStub()  # different stub so relation_exists() returns False
        charm = cast(ops.CharmBase, make_charm_from_relation(relation, interface_name="prometheus_scrape"))
        validator = PrometheusScrapeValidator(charm, cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"
        assert result.error is not None

    def test_fails_when_required_fields_missing(self) -> None:
        # GIVEN an empty databag
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "scrape_metadata" in schema_check.message
        assert "scrape_jobs" in schema_check.message

    def test_fails_when_scrape_metadata_missing(self) -> None:
        # GIVEN a databag with only scrape_jobs
        validator = _make_validator({"scrape_jobs": VALID_SCRAPE_JOBS})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "scrape_metadata" in schema_check.message

    def test_fails_when_scrape_metadata_not_valid_json(self) -> None:
        # GIVEN a databag with invalid JSON in scrape_metadata
        validator = _make_validator({"scrape_metadata": "not-json", "scrape_jobs": VALID_SCRAPE_JOBS})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "scrape_metadata" in schema_check.message

    def test_fails_when_scrape_metadata_missing_required_keys(self) -> None:
        # GIVEN scrape_metadata that is valid JSON but missing 'model' and 'application'
        partial_meta = json.dumps({"model_uuid": "abc", "unit": "app/0"})
        validator = _make_validator({"scrape_metadata": partial_meta, "scrape_jobs": VALID_SCRAPE_JOBS})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "model" in schema_check.message

    def test_fails_when_scrape_jobs_not_valid_json(self) -> None:
        # GIVEN a databag with invalid JSON in scrape_jobs
        validator = _make_validator({"scrape_metadata": VALID_SCRAPE_METADATA, "scrape_jobs": "not-json"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "scrape_jobs" in schema_check.message

    def test_fails_when_scrape_jobs_is_empty_list(self) -> None:
        # GIVEN scrape_jobs is a valid but empty JSON list
        validator = _make_validator({"scrape_metadata": VALID_SCRAPE_METADATA, "scrape_jobs": "[]"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        jobs_check = next(c for c in result.checks if c.name == "scrape_jobs")
        assert not jobs_check.passed

    def test_fails_when_scrape_jobs_missing_static_configs(self) -> None:
        # GIVEN a scrape job with no static_configs key
        jobs = json.dumps([{"metrics_path": "/metrics"}])
        validator = _make_validator({"scrape_metadata": VALID_SCRAPE_METADATA, "scrape_jobs": jobs})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        jobs_check = next(c for c in result.checks if c.name == "scrape_jobs")
        assert not jobs_check.passed
        assert "static_configs" in jobs_check.message

    def test_passes_with_valid_databag(self) -> None:
        # GIVEN a complete valid databag
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed
        jobs_check = next(c for c in result.checks if c.name == "scrape_jobs")
        assert jobs_check.passed

    def test_skips_for_provides_role(self) -> None:
        # GIVEN a validator running on the provides side (e.g. alertmanager-k8s)
        # with a valid databag on the remote side — we still want SKIPPED, not FAIL
        app = ApplicationStub()
        relation = RelationStub(name="metrics-endpoint", id=0, app=app, data={app: VALID_DATABAG})
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="prometheus_scrape", role=RelationRoleStub.provides),
        )
        validator = PrometheusScrapeValidator(charm, cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "SKIPPED"
        assert result.checks == []
        assert result.error is not None
        assert "provides" in result.error
        assert result.role == "provides"

    def test_skips_for_peer_role(self) -> None:
        # GIVEN a validator running on a peer relation
        app = ApplicationStub()
        relation = RelationStub(name="metrics-endpoint", id=0, app=app, data={app: VALID_DATABAG})
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="prometheus_scrape", role=RelationRoleStub.peer),
        )
        validator = PrometheusScrapeValidator(charm, cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "SKIPPED"
        assert result.checks == []
        assert result.error is not None
        assert "peer" in result.error
        assert result.role == "peer"

    def test_skips_for_provides_role_at_deep_level(self) -> None:
        # GIVEN a provides-side validator at deep level — still skips, not FAIL
        app = ApplicationStub()
        relation = RelationStub(name="metrics-endpoint", id=0, app=app, data={app: VALID_DATABAG})
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, interface_name="prometheus_scrape", role=RelationRoleStub.provides),
        )
        validator = PrometheusScrapeValidator(charm, cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "SKIPPED"
        assert result.checks == []
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-endpoint")

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-endpoint"
        assert result.interface == "prometheus_scrape"


# ---------------------------------------------------------------------------
# Tests: deep level
# ---------------------------------------------------------------------------


class TestPrometheusScrapeValidatorDeep:
    def test_passes_when_targets_reachable(self) -> None:
        # GIVEN a valid databag and all targets are TCP-reachable
        validator = _make_validator(VALID_DATABAG)

        with patch("validators.prometheus_scrape.validator._tcp_ping"):
            result = validator.validate(level="deep")

        assert result.status == "PASS"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert connect_check.passed

    def test_fails_when_target_unreachable(self) -> None:
        # GIVEN a valid databag but a target that refuses TCP connections
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.prometheus_scrape.validator._tcp_ping",
            side_effect=socket.timeout("timed out"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "timed out" in connect_check.message

    def test_fails_schema_check_stops_before_connectivity(self) -> None:
        # GIVEN an empty databag (schema fails)
        validator = _make_validator({})

        with patch("validators.prometheus_scrape.validator._tcp_ping") as mock_ping:
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        mock_ping.assert_not_called()

    def test_no_connect_check_in_simple_level(self) -> None:
        # GIVEN a valid databag at simple level (no TCP connectivity check)
        validator = _make_validator(VALID_DATABAG)

        result = validator.validate(level="simple")

        assert result.status == "PASS"
        assert not any(c.name == "connect" for c in result.checks)
