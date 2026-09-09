# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import os
from typing import cast
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import ops
import pytest

from validators.istio_ingress_route.validator import IstioIngressRouteValidator, _build_opener, _NoRedirectHandler
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Helpers / factory
# ---------------------------------------------------------------------------

# Provider (istio-ingress-k8s) app databag: TLS disabled -> http URL
VALID_HTTP_DATABAG: dict[str, str] = {
    "external_host": "10.64.140.43",
    "tls_enabled": "False",
}

# Provider app databag: TLS enabled -> https URL
VALID_HTTPS_DATABAG: dict[str, str] = {
    "external_host": "ingress.example.com",
    "tls_enabled": "True",
}

# Requirer's own local databag on this relation: declares an HTTP listener on 8080,
# per the upstream integration tester (see validator module docstring).
DEFAULT_LOCAL_DATABAG: dict[str, str] = {
    "config": json.dumps({"model": "test-model", "listeners": [{"port": 8080, "protocol": "HTTP"}]}),
}


def _make_validator(
    app_databag: dict[str, str],
    endpoint: str = "ingress",
    role: RelationRoleStub = RelationRoleStub.requires,
    local_databag: dict[str, str] | None = None,
) -> IstioIngressRouteValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: app_databag})
    charm = make_charm_from_relation(relation, role=role, interface_name="istio_ingress_route")
    # The requirer publishes its own listener config into its own local app databag.
    relation.data[charm.app] = DEFAULT_LOCAL_DATABAG if local_databag is None else local_databag
    return IstioIngressRouteValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


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


class TestIstioIngressRouteValidatorSimple:
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
        validator = _make_validator(VALID_HTTP_DATABAG, role=role)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

    def test_skipped_for_unsupported_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_HTTP_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_error_when_relation_app_is_none(self) -> None:
        # GIVEN a relation with no remote application
        relation = RelationStub(name="ingress", id=0, app=None)
        anchor = RelationStub(name="ingress", id=0, app=ApplicationStub())
        charm = make_charm_from_relation(anchor, role=RelationRoleStub.requires, interface_name="istio_ingress_route")
        validator = IstioIngressRouteValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"

    def test_fails_schema_when_external_host_missing(self) -> None:
        # GIVEN provider databag has no 'external_host'
        validator = _make_validator({"tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "external_host" in schema.message

    def test_fails_schema_when_tls_enabled_missing(self) -> None:
        # GIVEN provider databag has no 'tls_enabled'
        validator = _make_validator({"external_host": "10.64.140.43"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "tls_enabled" in schema.message

    def test_fails_schema_when_tls_enabled_is_not_a_bool_string(self) -> None:
        # GIVEN 'tls_enabled' is neither 'True' nor 'False'
        validator = _make_validator({"external_host": "10.64.140.43", "tls_enabled": "yes"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "tls_enabled" in schema.message

    def test_fails_url_format_when_external_host_has_invalid_port(self) -> None:
        # GIVEN external_host encodes an out-of-range port number
        validator = _make_validator({"external_host": "10.64.140.43:99999", "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        fmt = next(c for c in result.checks if c.name == "url_format")
        assert not fmt.passed
        assert "invalid port" in fmt.message

    @pytest.mark.parametrize(
        "external_host",
        [
            "bad host",  # embedded whitespace
            "host\x00name",  # control character
            "host?query=1",  # query component smuggled into the host
            "user@host",  # user-info component smuggled into the host
            "-leading-hyphen.example.com",  # invalid DNS label
            "good.example\n",  # trailing newline, silently stripped by urlparse
            "good.example\r",  # trailing carriage return, silently stripped by urlparse
            "good.example\t",  # embedded tab, silently stripped by urlparse
            "upstream.example.com/model app",  # unescaped space in an otherwise-allowed path
            "ingress.example.com..",  # multiple trailing dots are not a valid FQDN
            "example.com/café",  # raw non-ASCII char; urllib requires an ASCII URI
            "@host",  # syntactically-present but empty user-info; username == '' not None
            "host?",  # syntactically-present but empty query; query attribute == ''
            "host#",  # syntactically-present but empty fragment; fragment attribute == ''
        ],
    )
    def test_fails_url_format_when_external_host_is_malformed(self, external_host: str) -> None:
        # GIVEN a malformed external_host that is not a bare host[:port]
        validator = _make_validator({"external_host": external_host, "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        fmt = next(c for c in result.checks if c.name == "url_format")
        assert not fmt.passed

    def test_passes_simple_with_tls_disabled(self) -> None:
        # GIVEN valid provider databag with TLS disabled
        validator = _make_validator(VALID_HTTP_DATABAG)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema = next(c for c in result.checks if c.name == "schema")
        assert schema.passed

    def test_passes_simple_with_tls_enabled(self) -> None:
        # GIVEN valid provider databag with TLS enabled
        validator = _make_validator(VALID_HTTPS_DATABAG)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"

    def test_passes_simple_when_external_host_carries_upstream_route_path(self) -> None:
        # GIVEN external_host preserves a path from an upstream ingress hop
        # (e.g. istio-ingress chained behind another ingress), as some providers do
        validator = _make_validator({"external_host": "upstream.example.com/model-app", "tls_enabled": "True"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN the routed path is accepted; only query/fragment/user-info are rejected
        assert result.status == "PASS"
        fmt = next(c for c in result.checks if c.name == "url_format")
        assert fmt.passed

    def test_passes_simple_when_external_host_is_an_absolute_fqdn(self) -> None:
        # GIVEN external_host is an absolute (fully-qualified) DNS name with a
        # trailing root dot, which is valid and resolvable
        validator = _make_validator({"external_host": "ingress.example.com.", "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN the single trailing dot is accepted
        assert result.status == "PASS"
        fmt = next(c for c in result.checks if c.name == "url_format")
        assert fmt.passed

    def test_redacts_user_info_from_result_messages(self) -> None:
        # GIVEN external_host smuggles a credential via user-info
        validator = _make_validator({"external_host": "user:secret@host", "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN the secret never appears in any check message
        assert result.status == "FAIL"
        assert all("secret" not in c.message for c in result.checks)

    def test_redacts_query_string_from_result_messages(self) -> None:
        # GIVEN external_host smuggles a credential via a query string
        validator = _make_validator({"external_host": "host/path?token=secret", "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN the secret never appears in any check message
        assert result.status == "FAIL"
        assert all("secret" not in c.message for c in result.checks)

    def test_redacts_user_info_with_embedded_at_sign_from_result_messages(self) -> None:
        # GIVEN a password containing an embedded '@', so the user-info component has
        # more than one '@' character (e.g. "user:first-secret@second-secret@host")
        validator = _make_validator({"external_host": "user:first-secret@second-secret@host", "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN redaction splits at the *last* '@' (the real userinfo/host boundary), so
        # neither part of the password ever appears in any check message
        assert result.status == "FAIL"
        assert all("secret" not in c.message for c in result.checks)

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_HTTP_DATABAG, endpoint="my-ingress")

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-ingress"
        assert result.interface == "istio_ingress_route"


# ---------------------------------------------------------------------------
# Deep level tests
# ---------------------------------------------------------------------------


class TestIstioIngressRouteValidatorDeep:
    def test_passes_deep_with_tcp_reachable_and_http_ok(self) -> None:
        # GIVEN reachable endpoint returning HTTP 200
        validator = _make_validator(VALID_HTTP_DATABAG)

        with (
            patch("validators.istio_ingress_route.validator._tcp_ping"),
            patch(
                "validators.istio_ingress_route.validator._opener.open",
                return_value=_mock_http_response(200),
            ),
        ):
            result = validator.validate(level="deep")

        assert result.status == "PASS"
        assert any(c.name == "connect" and c.passed for c in result.checks)
        assert any(c.name == "http_probe" and c.passed for c in result.checks)

    def test_passes_http_probe_when_server_returns_4xx(self) -> None:
        # GIVEN endpoint returns 404 — still proves the ingress gateway is routing
        validator = _make_validator(VALID_HTTP_DATABAG)

        with (
            patch("validators.istio_ingress_route.validator._tcp_ping"),
            patch(
                "validators.istio_ingress_route.validator._opener.open",
                side_effect=HTTPError(
                    "http://10.64.140.43",
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
        validator = _make_validator(VALID_HTTP_DATABAG)

        with patch(
            "validators.istio_ingress_route.validator._tcp_ping",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        connect = next(c for c in result.checks if c.name == "connect")
        assert not connect.passed

    def test_no_redirect_handler_raises_instead_of_following(self) -> None:
        # GIVEN a 302 response naming an unrelated/unreachable redirect target
        handler = _NoRedirectHandler()
        req = Request("http://10.64.140.43")

        # WHEN/THEN the handler must not hand back a Request to follow
        with pytest.raises(HTTPError):
            handler.redirect_request(
                req,
                None,  # type: ignore[arg-type]
                302,
                "Found",
                {"location": "http://unrelated.example"},  # type: ignore[arg-type]
                "http://unrelated.example",
            )

    def test_http_probe_treats_redirect_as_reachable_without_following(self) -> None:
        # GIVEN the gateway responds with a redirect — proves it is live, but must
        # not be followed to the (possibly unreachable/unrelated) redirect target
        validator = _make_validator(VALID_HTTP_DATABAG)

        with (
            patch("validators.istio_ingress_route.validator._tcp_ping"),
            patch(
                "validators.istio_ingress_route.validator._opener.open",
                side_effect=HTTPError(
                    "http://10.64.140.43",
                    302,
                    "Found",
                    {"location": "http://unrelated.example"},  # type: ignore[arg-type]
                    None,
                ),
            ),
        ):
            result = validator.validate(level="deep")

        assert result.status == "PASS"
        probe = next(c for c in result.checks if c.name == "http_probe")
        assert probe.passed
        assert "302" in probe.message

    def test_opener_disables_environment_proxies(self) -> None:
        # GIVEN a CI/dev-style environment advertising an HTTP(S) proxy
        with patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://proxy.example:3128", "HTTPS_PROXY": "http://proxy.example:3128"},
        ):
            # WHEN the opener is built via the actual production helper
            opener = _build_opener()

            # THEN no ProxyHandler is wired in, so the probe bypasses the env proxy
            assert not any(isinstance(h, ProxyHandler) for h in opener.handlers)  # type: ignore[attr-defined]

            # AND without the explicit override, the proxy would have been picked up —
            # proving the override, not an unrelated default, is what disables it
            default_style_opener = build_opener(_NoRedirectHandler)
            assert any(
                isinstance(h, ProxyHandler)
                for h in default_style_opener.handlers  # type: ignore[attr-defined]
            )

    def test_fails_deep_when_http_connection_refused(self) -> None:
        # GIVEN TCP succeeds but HTTP fails
        validator = _make_validator(VALID_HTTP_DATABAG)

        with (
            patch("validators.istio_ingress_route.validator._tcp_ping"),
            patch(
                "validators.istio_ingress_route.validator._opener.open",
                side_effect=URLError("Connection refused"),
            ),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        probe = next(c for c in result.checks if c.name == "http_probe")
        assert not probe.passed

    def test_http_probe_closes_http_error_response(self) -> None:
        # GIVEN endpoint returns an HTTP error (HTTPError is also a file-like response)
        validator = _make_validator(VALID_HTTP_DATABAG)

        mock_exc = HTTPError(
            "http://10.64.140.43",
            503,
            "Service Unavailable",
            {},  # type: ignore[arg-type]
            None,
        )
        mock_exc.close = MagicMock()

        with (
            patch("validators.istio_ingress_route.validator._tcp_ping"),
            patch("validators.istio_ingress_route.validator._opener.open", side_effect=mock_exc),
        ):
            result = validator.validate(level="deep")

        # THEN the response is closed to release the underlying socket
        mock_exc.close.assert_called_once()
        probe = next(c for c in result.checks if c.name == "http_probe")
        assert probe.passed
        assert "503" in probe.message

    def test_deep_skipped_for_uat_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_HTTP_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_deep_schema_fail_prevents_connectivity_check(self) -> None:
        # GIVEN missing fields — should fail at schema before reaching connect
        validator = _make_validator({})

        result = validator.validate(level="deep")

        assert result.status == "FAIL"
        assert not any(c.name == "connect" for c in result.checks)

    def test_deep_probes_port_declared_in_local_listener_config(self) -> None:
        # GIVEN external_host carries no port, and the requirer's own local config
        # declares an HTTP listener on a non-default port (8080, as the upstream
        # integration tester does), not 80/443
        validator = _make_validator(
            VALID_HTTP_DATABAG,
            local_databag={"config": json.dumps({"model": "m", "listeners": [{"port": 8080, "protocol": "HTTP"}]})},
        )

        with (
            patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping,
            patch(
                "validators.istio_ingress_route.validator._opener.open",
                return_value=_mock_http_response(200),
            ) as opener_open,
        ):
            result = validator.validate(level="deep")

        # THEN the declared listener port is used, not a default of 80/443
        assert result.status == "PASS"
        tcp_ping.assert_called_once_with("10.64.140.43", 8080)
        probed_request = opener_open.call_args[0][0]
        assert probed_request.full_url == "http://10.64.140.43:8080"

    def test_deep_prefers_explicit_port_in_external_host_over_local_config(self) -> None:
        # GIVEN external_host itself already encodes a port, which is unambiguous
        validator = _make_validator(
            {"external_host": "10.64.140.43:9999", "tls_enabled": "False"},
            local_databag={"config": json.dumps({"model": "m", "listeners": [{"port": 8080, "protocol": "HTTP"}]})},
        )

        with (
            patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping,
            patch(
                "validators.istio_ingress_route.validator._opener.open",
                return_value=_mock_http_response(200),
            ),
        ):
            result = validator.validate(level="deep")

        # THEN the explicit port wins over the locally-declared listener config
        assert result.status == "PASS"
        tcp_ping.assert_called_once_with("10.64.140.43", 9999)

    def test_deep_skips_connectivity_when_no_local_config_published(self) -> None:
        # GIVEN the requirer has not (yet) published its own listener config
        validator = _make_validator(VALID_HTTP_DATABAG, local_databag={})

        with patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping:
            result = validator.validate(level="deep")

        # THEN the connectivity/probe checks are skipped rather than guessing a port
        assert result.status == "PASS"
        tcp_ping.assert_not_called()
        assert not any(c.name == "http_probe" for c in result.checks)
        connect = next(c for c in result.checks if c.name == "connect")
        assert connect.passed

    def test_deep_skips_connectivity_when_local_config_has_no_http_listener(self) -> None:
        # GIVEN the requirer only declared a GRPC listener, which an HTTP GET probe
        # would not meaningfully exercise
        validator = _make_validator(
            VALID_HTTP_DATABAG,
            local_databag={"config": json.dumps({"model": "m", "listeners": [{"port": 9090, "protocol": "GRPC"}]})},
        )

        with patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping:
            result = validator.validate(level="deep")

        assert result.status == "PASS"
        tcp_ping.assert_not_called()
        assert not any(c.name == "http_probe" for c in result.checks)

    def test_deep_fails_when_local_config_is_malformed(self) -> None:
        # GIVEN the requirer's local 'config' is not valid JSON
        validator = _make_validator(VALID_HTTP_DATABAG, local_databag={"config": "not-json"})

        with patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping:
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        tcp_ping.assert_not_called()
        connect = next(c for c in result.checks if c.name == "connect")
        assert not connect.passed
