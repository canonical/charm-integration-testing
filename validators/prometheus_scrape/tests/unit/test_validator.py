# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from typing import cast
from unittest.mock import MagicMock, patch

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


def _mock_http_response(status: int = 200, body: bytes = b"") -> MagicMock:
    """Return a context-manager mock that yields an HTTP response with the given status and body."""
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.status = status
    resp.read.return_value = body
    return resp


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

PROMETHEUS_TEXT_BODY = (
    b"# HELP go_goroutines Number of goroutines that currently exist.\n"
    b"# TYPE go_goroutines gauge\n"
    b"go_goroutines 42\n"
    b"# HELP process_cpu_seconds_total Total user and system CPU time spent in seconds.\n"
    b"# TYPE process_cpu_seconds_total counter\n"
    b"process_cpu_seconds_total 0.05\n"
)


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

    def test_fails_when_scrape_metadata_not_a_dict(self) -> None:
        # GIVEN scrape_metadata that is valid JSON but not a dict (e.g. a list)
        validator = _make_validator({"scrape_metadata": "[]", "scrape_jobs": VALID_SCRAPE_JOBS})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "must be a JSON object" in schema_check.message

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

    def test_fails_when_http_probe_returns_non_200(self) -> None:
        # GIVEN a valid databag but the metrics endpoint returns HTTP 404
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.prometheus_scrape.validator.urlopen",
            return_value=_mock_http_response(404),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        http_check = next(c for c in result.checks if c.name == "http_probe")
        assert not http_check.passed
        assert "404" in http_check.message

    def test_fails_when_http_probe_connection_error(self) -> None:
        # GIVEN a valid databag but the target is unreachable
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.prometheus_scrape.validator.urlopen",
            side_effect=ConnectionRefusedError("connection refused"),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        http_check = next(c for c in result.checks if c.name == "http_probe")
        assert not http_check.passed
        assert "connection refused" in http_check.message

    def test_passes_with_valid_databag(self) -> None:
        # GIVEN a complete valid databag and a reachable metrics endpoint
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.prometheus_scrape.validator.urlopen",
            return_value=_mock_http_response(200),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed
        jobs_check = next(c for c in result.checks if c.name == "scrape_jobs")
        assert jobs_check.passed
        http_check = next(c for c in result.checks if c.name == "http_probe")
        assert http_check.passed

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

    def test_result_contains_endpoint_and_interface(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-endpoint")

        with patch(
            "validators.prometheus_scrape.validator.urlopen",
            return_value=_mock_http_response(200),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-endpoint"
        assert result.interface == "prometheus_scrape"

    def test_simple_level_has_http_probe_but_no_deep_checks(self) -> None:
        # GIVEN a valid databag at simple level
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.prometheus_scrape.validator.urlopen",
            return_value=_mock_http_response(200),
        ):
            result = validator.validate(level="simple")

        # THEN: simple level has http_probe but none of the L2 checks
        assert any(c.name == "http_probe" for c in result.checks)
        assert not any(c.name.startswith("scrape[") for c in result.checks)
        assert not any(c.name == "labels" for c in result.checks)


# ---------------------------------------------------------------------------
# Tests: deep level
# ---------------------------------------------------------------------------


class TestPrometheusScrapeValidatorDeep:
    def test_passes_with_valid_metrics_response(self) -> None:
        # GIVEN a valid databag and a target that returns valid Prometheus text
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.prometheus_scrape.validator.urlopen",
            return_value=_mock_http_response(200, PROMETHEUS_TEXT_BODY),
        ):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        assert any(c.name == "http_probe" and c.passed for c in result.checks)
        scrape_checks = [c for c in result.checks if c.name.startswith("scrape[")]
        assert scrape_checks and all(c.passed for c in scrape_checks)
        labels_check = next(c for c in result.checks if c.name == "labels")
        assert labels_check.passed

    def test_fails_when_http_probe_unreachable(self) -> None:
        # GIVEN a valid databag but the target refuses connections
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.prometheus_scrape.validator.urlopen",
            side_effect=ConnectionRefusedError("connection refused"),
        ):
            result = validator.validate(level="deep")

        # THEN: schema/jobs pass but http_probe fails; deep checks are not run
        assert result.status == "FAIL"
        http_check = next(c for c in result.checks if c.name == "http_probe")
        assert not http_check.passed
        assert not any(c.name.startswith("scrape[") for c in result.checks)

    def test_fails_when_metrics_response_has_no_metric_families(self) -> None:
        # GIVEN a target that returns 200 OK but an empty body (no metrics)
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.prometheus_scrape.validator.urlopen",
            return_value=_mock_http_response(200, b""),
        ):
            result = validator.validate(level="deep")

        # THEN: http_probe passes (200 OK), but scrape check fails (no metric families)
        assert result.status == "FAIL"
        http_check = next(c for c in result.checks if c.name == "http_probe")
        assert http_check.passed
        scrape_checks = [c for c in result.checks if c.name.startswith("scrape[")]
        assert scrape_checks and not all(c.passed for c in scrape_checks)

    def test_fails_when_static_labels_have_invalid_names(self) -> None:
        # GIVEN a scrape job with static labels that contain invalid Prometheus label names
        jobs = json.dumps(
            [
                {
                    "metrics_path": "/metrics",
                    "static_configs": [
                        {
                            "targets": ["my-app-0.my-app.svc.cluster.local:8080"],
                            "labels": {"0invalid": "value", "valid_label": "ok"},
                        }
                    ],
                    "scheme": "http",
                }
            ]
        )
        validator = _make_validator({"scrape_metadata": VALID_SCRAPE_METADATA, "scrape_jobs": jobs})

        with patch(
            "validators.prometheus_scrape.validator.urlopen",
            return_value=_mock_http_response(200, PROMETHEUS_TEXT_BODY),
        ):
            result = validator.validate(level="deep")

        # THEN: labels check fails due to invalid label name
        assert result.status == "FAIL"
        labels_check = next(c for c in result.checks if c.name == "labels")
        assert not labels_check.passed
        assert "0invalid" in labels_check.message

    def test_passes_with_valid_static_labels(self) -> None:
        # GIVEN a scrape job with valid static labels
        jobs = json.dumps(
            [
                {
                    "metrics_path": "/metrics",
                    "static_configs": [
                        {
                            "targets": ["my-app-0.my-app.svc.cluster.local:8080"],
                            "labels": {"env": "production", "team": "platform"},
                        }
                    ],
                    "scheme": "http",
                }
            ]
        )
        validator = _make_validator({"scrape_metadata": VALID_SCRAPE_METADATA, "scrape_jobs": jobs})

        with patch(
            "validators.prometheus_scrape.validator.urlopen",
            return_value=_mock_http_response(200, PROMETHEUS_TEXT_BODY),
        ):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        labels_check = next(c for c in result.checks if c.name == "labels")
        assert labels_check.passed

    def test_schema_fail_stops_before_http(self) -> None:
        # GIVEN an empty databag (schema fails) — HTTP should never be called
        validator = _make_validator({})

        with patch("validators.prometheus_scrape.validator.urlopen") as mock_urlopen:
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        mock_urlopen.assert_not_called()
