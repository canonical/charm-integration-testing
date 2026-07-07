# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import base64
import json
import lzma
from typing import cast

import ops

from validators.grafana_dashboard.validator import GrafanaDashboardValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
    UnitStub,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_validator(
    provider_databag: dict[str, str],
    endpoint: str = "grafana-dashboard",
    role: RelationRoleStub = RelationRoleStub.requires,
    units: frozenset[UnitStub] | None = None,
) -> GrafanaDashboardValidator:
    """Build a GrafanaDashboardValidator with a remote (provider) app databag."""
    app = ApplicationStub()
    # Default to one active remote unit so units_present passes by default.
    remote_units: frozenset[UnitStub] = units if units is not None else frozenset({UnitStub()})
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: provider_databag}, units=remote_units)
    charm = cast(ops.CharmBase, make_charm_from_relation(relation, role=role, interface_name="grafana_dashboard"))
    return GrafanaDashboardValidator(charm, cast(ops.Relation, relation))


def _lzma_b64(data: dict[str, object]) -> str:
    """Compress a dict to LZMA+Base64 as the providing charm would."""
    raw = json.dumps(data).encode("utf-8")
    return base64.b64encode(lzma.compress(raw)).decode("utf-8")


def _valid_template(charm_name: str = "prometheus-k8s") -> dict[str, object]:
    return {
        "content": _lzma_b64({"title": "Test Dashboard", "panels": []}),
        "charm": charm_name,
        "juju_topology": {"model": "cos", "model_uuid": "abc-123", "application": "prom", "unit": "prom/0"},
        "inject_dropdowns": True,
    }


VALID_OUTER = {
    "templates": {"file:prometheus-overview": _valid_template()},
    "uuid": "550e8400-e29b-41d4-a716-446655440000",
}

VALID_DATABAG: dict[str, str] = {"dashboards": json.dumps(VALID_OUTER)}


# ---------------------------------------------------------------------------
# Tests: simple level
# ---------------------------------------------------------------------------


class TestGrafanaDashboardValidatorSimple:
    def test_returns_skipped_for_unsupported_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_returns_skipped_for_provides_role(self) -> None:
        # GIVEN a validator running on the provides side (a dashboard-sending charm)
        validator = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None
        assert "provides" in result.error

    def test_returns_skipped_for_peer_role(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, role=RelationRoleStub.peer)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None
        assert "peer" in result.error

    def test_returns_error_when_no_remote_app(self) -> None:
        # GIVEN a relation whose remote app is not present in relation.data
        app = ApplicationStub()
        relation = RelationStub(name="grafana-dashboard", id=0, app=app, data={app: {}})
        # Swap to a stub not in data so relation_exists() returns False.
        relation.app = ApplicationStub()
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation(relation, role=RelationRoleStub.requires, interface_name="grafana_dashboard"),
        )
        validator = GrafanaDashboardValidator(charm, cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"
        assert result.error is not None

    def test_fails_when_provider_has_no_units(self) -> None:
        # GIVEN a valid databag but the remote app has 0 active units
        validator = _make_validator(VALID_DATABAG, units=frozenset())

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        units_check = next(c for c in result.checks if c.name == "units_present")
        assert not units_check.passed
        assert "no active units" in units_check.message

    def test_fails_when_dashboards_key_absent(self) -> None:
        # GIVEN a completely empty provider databag
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "dashboards" in schema_check.message

    def test_fails_when_dashboards_is_not_valid_json(self) -> None:
        # GIVEN dashboards field contains non-JSON
        validator = _make_validator({"dashboards": "not-json"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "not valid JSON" in schema_check.message

    def test_fails_when_dashboards_is_not_a_dict(self) -> None:
        # GIVEN dashboards is a JSON array instead of object
        validator = _make_validator({"dashboards": "[]"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "JSON object" in schema_check.message

    def test_fails_when_templates_key_missing(self) -> None:
        # GIVEN outer object has no 'templates' key
        outer = {"uuid": "abc-123"}
        validator = _make_validator({"dashboards": json.dumps(outer)})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        structure_check = next(c for c in result.checks if c.name == "structure")
        assert not structure_check.passed
        assert "templates" in structure_check.message

    def test_fails_when_templates_is_empty(self) -> None:
        # GIVEN outer object has an empty templates dict
        outer = {"templates": {}, "uuid": "abc-123"}
        validator = _make_validator({"dashboards": json.dumps(outer)})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        structure_check = next(c for c in result.checks if c.name == "structure")
        assert not structure_check.passed

    def test_fails_when_template_missing_required_fields(self) -> None:
        # GIVEN a template with no 'content' or 'charm' keys
        outer = {
            "templates": {"file:test": {"inject_dropdowns": True}},
            "uuid": "abc-123",
        }
        validator = _make_validator({"dashboards": json.dumps(outer)})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        structure_check = next(c for c in result.checks if c.name == "structure")
        assert not structure_check.passed
        assert "content" in structure_check.message

    def test_passes_with_valid_databag(self) -> None:
        # GIVEN a complete, well-formed provider databag
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        assert next(c for c in result.checks if c.name == "schema").passed
        assert next(c for c in result.checks if c.name == "structure").passed

    def test_simple_level_has_no_content_check(self) -> None:
        # GIVEN a valid databag at simple level
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="simple")

        # THEN: content check only runs at deep level
        assert not any(c.name == "content" for c in result.checks)

    def test_result_contains_endpoint_and_interface(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-grafana-dashboard")

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-grafana-dashboard"
        assert result.interface == "grafana_dashboard"


# ---------------------------------------------------------------------------
# Tests: deep level
# ---------------------------------------------------------------------------


class TestGrafanaDashboardValidatorDeep:
    def test_passes_with_valid_lzma_content(self) -> None:
        # GIVEN a databag with correctly encoded dashboard content
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        assert next(c for c in result.checks if c.name == "content").passed

    def test_fails_when_content_is_not_base64(self) -> None:
        # GIVEN template content that is plain text, not LZMA+Base64
        outer = {
            "templates": {
                "file:test": {
                    "content": "this is not base64 encoded lzma",
                    "charm": "test-charm",
                    "juju_topology": {},
                    "inject_dropdowns": False,
                }
            },
            "uuid": "abc-123",
        }
        validator = _make_validator({"dashboards": json.dumps(outer)})

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        content_check = next(c for c in result.checks if c.name == "content")
        assert not content_check.passed
        assert "decode failed" in content_check.message

    def test_fails_when_content_is_not_valid_json_after_decode(self) -> None:
        # GIVEN template content that decodes from LZMA+Base64 but is not JSON
        not_json = base64.b64encode(lzma.compress(b"not a json string")).decode("utf-8")
        outer = {
            "templates": {
                "file:test": {
                    "content": not_json,
                    "charm": "test-charm",
                    "juju_topology": {},
                    "inject_dropdowns": False,
                }
            },
            "uuid": "abc-123",
        }
        validator = _make_validator({"dashboards": json.dumps(outer)})

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        content_check = next(c for c in result.checks if c.name == "content")
        assert not content_check.passed

    def test_fails_when_content_field_is_empty(self) -> None:
        # GIVEN a template with an empty content field
        outer = {
            "templates": {
                "file:test": {
                    "content": "",
                    "charm": "test-charm",
                    "juju_topology": {},
                    "inject_dropdowns": False,
                }
            },
            "uuid": "abc-123",
        }
        validator = _make_validator({"dashboards": json.dumps(outer)})

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        content_check = next(c for c in result.checks if c.name == "content")
        assert not content_check.passed
        assert "empty" in content_check.message

    def test_schema_fail_stops_before_content_check(self) -> None:
        # GIVEN an empty databag — schema fails, content check must not run
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        assert not any(c.name == "content" for c in result.checks)

    def test_multiple_templates_all_checked(self) -> None:
        # GIVEN two templates, one with bad content
        outer = {
            "templates": {
                "file:good": _valid_template(),
                "file:bad": {
                    "content": "notbase64!!!",
                    "charm": "other-charm",
                    "juju_topology": {},
                    "inject_dropdowns": False,
                },
            },
            "uuid": "abc-123",
        }
        validator = _make_validator({"dashboards": json.dumps(outer)})

        # WHEN
        result = validator.validate(level="deep")

        # THEN: overall FAIL because one template is invalid
        assert result.status == "FAIL"
        content_check = next(c for c in result.checks if c.name == "content")
        assert "file:bad" in content_check.message
