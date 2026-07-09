# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from typing import cast
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import ops
import pytest

from validators.ingress.validator import IngressValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Helpers / factory
# ---------------------------------------------------------------------------

# v2 wire format: JSON-encoded dict under "ingress" key
VALID_V2_DATABAG: dict[str, str] = {
    "ingress": json.dumps({"url": "http://10.0.0.1/ingress-test-grafana-k8s"}),
}

# v1 wire format (deprecated): YAML-encoded string under "ingress" key
VALID_V1_DATABAG: dict[str, str] = {
    "ingress": "url: http://10.0.0.1/ingress-test-grafana-k8s\n",
}


def _make_validator(
    app_databag: dict[str, str],
    endpoint: str = "ingress",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> IngressValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: app_databag})
    charm = make_charm_from_relation(relation, role=role, interface_name="ingress")
    return IngressValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def _mock_http_response(status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.status = status
    resp.read.return_value = b"OK"
    return resp


# ---------------------------------------------------------------------------
# Simple level tests
# ---------------------------------------------------------------------------


class TestIngressValidatorSimple:
    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.requires, False),
            (RelationRoleStub.provides, True),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_skipped_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN
        validator = _make_validator(VALID_V2_DATABAG, role=role)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

    def test_skipped_for_unsupported_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_V2_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_error_when_relation_app_is_none(self) -> None:
        # GIVEN a relation with no remote application
        relation = RelationStub(name="ingress", id=0, app=None)
        anchor = RelationStub(name="ingress", id=0, app=ApplicationStub())
        charm = make_charm_from_relation(anchor, role=RelationRoleStub.requires, interface_name="ingress")
        validator = IngressValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"

    def test_fails_schema_when_ingress_key_missing(self) -> None:
        # GIVEN provider databag has no 'ingress' key
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "Missing 'ingress'" in schema.message

    def test_fails_schema_when_ingress_value_not_decodable(self) -> None:
        # GIVEN provider databag has malformed 'ingress' value
        validator = _make_validator({"ingress": "not-json-or-yaml-dict"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed

    def test_fails_schema_when_url_key_missing(self) -> None:
        # GIVEN 'ingress' decodes correctly but has no 'url' key
        validator = _make_validator({"ingress": json.dumps({"other": "value"})})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "'url' key is missing" in schema.message

    def test_fails_schema_when_url_value_is_not_a_string(self) -> None:
        # GIVEN 'url' is a non-string type (e.g. integer from malformed provider)
        validator = _make_validator({"ingress": json.dumps({"url": 123})})

        # WHEN
        result = validator.validate(level="simple")

        # THEN — should be a schema failure, not a confusing url_format failure
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "int" in schema.message

    def test_fails_url_format_when_scheme_is_invalid(self) -> None:
        # GIVEN ingress URL has unsupported scheme
        validator = _make_validator({"ingress": json.dumps({"url": "ftp://10.0.0.1/path"})})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        fmt = next(c for c in result.checks if c.name == "url_format")
        assert not fmt.passed
        assert "ftp" in fmt.message

    def test_fails_url_format_when_port_is_out_of_range(self) -> None:
        # GIVEN ingress URL has an out-of-range port number
        validator = _make_validator({"ingress": json.dumps({"url": "http://10.0.0.1:99999/path"})})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        fmt = next(c for c in result.checks if c.name == "url_format")
        assert not fmt.passed
        assert "invalid port" in fmt.message

    def test_passes_simple_with_valid_v2_databag(self) -> None:
        # GIVEN valid v2 databag
        validator = _make_validator(VALID_V2_DATABAG)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema = next(c for c in result.checks if c.name == "schema")
        assert schema.passed

    def test_passes_simple_with_valid_v1_yaml_databag(self) -> None:
        # GIVEN valid v1 (YAML-encoded) databag
        validator = _make_validator(VALID_V1_DATABAG)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_V2_DATABAG, endpoint="my-ingress")

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-ingress"
        assert result.interface == "ingress"


# ---------------------------------------------------------------------------
# Deep level tests
# ---------------------------------------------------------------------------


class TestIngressValidatorDeep:
    def test_passes_deep_with_tcp_reachable_and_http_ok(self) -> None:
        # GIVEN reachable endpoint returning HTTP 200
        validator = _make_validator(VALID_V2_DATABAG)

        with (
            patch("validators.ingress.validator._tcp_ping"),
            patch(
                "validators.ingress.validator.urlopen",
                return_value=_mock_http_response(200),
            ),
        ):
            result = validator.validate(level="deep")

        assert result.status == "PASS"
        assert any(c.name == "connect" and c.passed for c in result.checks)
        assert any(c.name == "http_probe" and c.passed for c in result.checks)

    def test_passes_http_probe_when_server_returns_4xx(self) -> None:
        # GIVEN endpoint returns 404 — still proves ingress is active
        validator = _make_validator(VALID_V2_DATABAG)

        with (
            patch("validators.ingress.validator._tcp_ping"),
            patch(
                "validators.ingress.validator.urlopen",
                side_effect=HTTPError(
                    "http://10.0.0.1/ingress-test-grafana-k8s",
                    404,
                    "Not Found",
                    {},  # type: ignore[arg-type]
                    None,
                ),
            ),
        ):
            result = validator.validate(level="deep")

        assert result.status == "PASS"
        probe = next(c for c in result.checks if c.name == "http_probe")
        assert probe.passed
        assert "404" in probe.message

    def test_fails_deep_when_tcp_unreachable(self) -> None:
        # GIVEN TCP connection fails
        validator = _make_validator(VALID_V2_DATABAG)

        with patch(
            "validators.ingress.validator._tcp_ping",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        connect = next(c for c in result.checks if c.name == "connect")
        assert not connect.passed

    def test_fails_deep_when_http_connection_refused(self) -> None:
        # GIVEN TCP succeeds but HTTP fails
        validator = _make_validator(VALID_V2_DATABAG)

        with (
            patch("validators.ingress.validator._tcp_ping"),
            patch(
                "validators.ingress.validator.urlopen",
                side_effect=URLError("Connection refused"),
            ),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        probe = next(c for c in result.checks if c.name == "http_probe")
        assert not probe.passed

    def test_http_probe_closes_http_error_response(self) -> None:
        # GIVEN endpoint returns an HTTP error (HTTPError is also a file-like response)
        validator = _make_validator(VALID_V2_DATABAG)

        mock_exc = HTTPError(
            "http://10.0.0.1/ingress-test-grafana-k8s",
            503,
            "Service Unavailable",
            {},  # type: ignore[arg-type]
            None,
        )
        mock_exc.close = MagicMock()

        with (
            patch("validators.ingress.validator._tcp_ping"),
            patch("validators.ingress.validator.urlopen", side_effect=mock_exc),
        ):
            result = validator.validate(level="deep")

        # THEN the response is closed to release the underlying socket
        mock_exc.close.assert_called_once()
        probe = next(c for c in result.checks if c.name == "http_probe")
        assert probe.passed
        assert "503" in probe.message

    def test_deep_skipped_for_uat_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_V2_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_deep_schema_fail_prevents_connectivity_check(self) -> None:
        # GIVEN missing ingress key — should fail at schema before reaching connect
        validator = _make_validator({})

        result = validator.validate(level="deep")

        assert result.status == "FAIL"
        assert not any(c.name == "connect" for c in result.checks)
