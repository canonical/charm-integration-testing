# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import datetime
import json
import os
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

import ops
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

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

    def test_redacts_invalid_port_text_from_result_messages(self) -> None:
        # GIVEN external_host smuggles a secret disguised as a port (this only raises
        # ValueError, and thus is only caught, once parsed.port is actually accessed;
        # both the raw URL and the raised exception's text would otherwise echo it back)
        validator = _make_validator({"external_host": "host:super-secret-token", "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN the secret never appears in any check message
        assert result.status == "FAIL"
        fmt = next(c for c in result.checks if c.name == "url_format")
        assert not fmt.passed
        assert all("super-secret-token" not in c.message for c in result.checks)
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
            "host/path;token=secret",  # semicolon params on the final path segment
            "https://ingress.example.com",  # a scheme, not the bare host the interface publishes
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

    def test_redacts_non_ascii_digit_port_without_raising(self) -> None:
        # GIVEN external_host has a port-like segment containing a non-ASCII digit
        # character (str.isdigit() is True for '\u00b2' but int() rejects it, which
        # previously escaped as an unhandled exception from _redact instead of the
        # intended FAIL result)
        validator = _make_validator({"external_host": "host:12\u00b23", "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN validation completes normally (no unhandled exception) and rejects the
        # malformed port as a FAIL, with the raw port text redacted from every message
        assert result.status == "FAIL"
        fmt = next(c for c in result.checks if c.name == "url_format")
        assert not fmt.passed
        assert all("12\u00b23" not in c.message for c in result.checks)

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

    def test_passes_and_does_not_corrupt_bracketed_ipv6_host_without_port(self) -> None:
        # GIVEN a bracketed IPv6 external_host with no port (its own colons must not
        # be mistaken for a port separator when building the redacted display value)
        validator = _make_validator({"external_host": "[2001:db8::1]", "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN the host is accepted, and every message shows it intact/unredacted
        assert result.status == "PASS"
        assert any("[2001:db8::1]" in c.message for c in result.checks)

    def test_redacts_invalid_port_on_bracketed_ipv6_host_without_corrupting_address(self) -> None:
        # GIVEN a bracketed IPv6 host with a genuinely invalid port after the bracket
        validator = _make_validator({"external_host": "[2001:db8::1]:99999", "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN only the invalid port is redacted; the address itself is left intact
        assert result.status == "FAIL"
        assert any("[2001:db8::1]:<redacted>" in c.message for c in result.checks)

    def test_redacts_entire_malformed_suffix_after_first_colon_in_unbracketed_authority(self) -> None:
        # GIVEN an unbracketed authority with more than one colon: the last segment
        # ("80") looks like a valid port, so splitting at the *last* colon would leave
        # the earlier "super-secret" segment in the displayed value even though
        # parsed.port rejects this authority as a whole
        validator = _make_validator({"external_host": "host:super-secret:80", "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN the secret never appears in any check message
        assert result.status == "FAIL"
        assert all("super-secret" not in c.message for c in result.checks)

    def test_redacts_query_string_from_result_messages(self) -> None:
        # GIVEN external_host smuggles a credential via a query string
        validator = _make_validator({"external_host": "host/path?token=secret", "tls_enabled": "False"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN the secret never appears in any check message
        assert result.status == "FAIL"
        assert all("secret" not in c.message for c in result.checks)

    def test_redacts_path_segment_parameter_from_result_messages(self) -> None:
        # GIVEN external_host smuggles a credential via a semicolon path-segment
        # parameter on the final path segment (urlparse() strips this into
        # parsed.params rather than parsed.path)
        validator = _make_validator({"external_host": "host/path;token=secret", "tls_enabled": "False"})

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

    def test_deep_probes_default_port_for_chained_deployment_with_route_path(self) -> None:
        # GIVEN external_host carries an upstream route path (a chained deployment,
        # e.g. istio-ingress behind another ingress hop), and the requirer also
        # declares a local listener port for its *inner* gateway (which does not
        # apply to this outer hop)
        validator = _make_validator(
            {"external_host": "upstream.example.com/model-app", "tls_enabled": "True"},
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

        # THEN the scheme's conventional external port (443 for https) is probed, not
        # the inner listener's 8080, and the route path is preserved in the probe URL
        assert result.status == "PASS"
        tcp_ping.assert_called_once_with("upstream.example.com", 443)
        probed_request = opener_open.call_args[0][0]
        assert probed_request.full_url == "https://upstream.example.com:443/model-app"

    def test_deep_probes_default_http_port_for_chained_deployment_without_tls(self) -> None:
        # GIVEN the same chained-deployment scenario, but over plain http
        validator = _make_validator(
            {"external_host": "upstream.example.com/model-app", "tls_enabled": "False"},
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

        assert result.status == "PASS"
        tcp_ping.assert_called_once_with("upstream.example.com", 80)

    def test_deep_skips_connectivity_when_no_local_config_published(self) -> None:
        # GIVEN the requirer has not (yet) published its own listener config
        validator = _make_validator(VALID_HTTP_DATABAG, local_databag={})

        with patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping:
            result = validator.validate(level="deep")

        # THEN the connectivity/probe checks are skipped rather than guessing a port,
        # and the overall result reports SKIPPED (not a false PASS) since neither the
        # TCP nor HTTP capability check actually ran
        assert result.status == "SKIPPED"
        tcp_ping.assert_not_called()
        assert not any(c.name == "http_probe" for c in result.checks)
        connect = next(c for c in result.checks if c.name == "connect")
        assert connect.passed

    def test_deep_fails_when_local_config_is_published_but_empty(self) -> None:
        # GIVEN 'config' is present on the local databag but an empty string, which is
        # a different condition from the key being absent altogether: the requirer
        # published something that is not valid JSON, so this must FAIL, not be
        # treated the same as "nothing published yet" (SKIPPED)
        validator = _make_validator(VALID_HTTP_DATABAG, local_databag={"config": ""})

        with patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping:
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        tcp_ping.assert_not_called()
        connect = next(c for c in result.checks if c.name == "connect")
        assert not connect.passed

    def test_deep_skips_connectivity_when_local_config_has_no_http_listener(self) -> None:
        # GIVEN the requirer only declared a GRPC listener, which an HTTP GET probe
        # would not meaningfully exercise
        validator = _make_validator(
            VALID_HTTP_DATABAG,
            local_databag={"config": json.dumps({"model": "m", "listeners": [{"port": 9090, "protocol": "GRPC"}]})},
        )

        with patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping:
            result = validator.validate(level="deep")

        # THEN HTTP probing is inapplicable, so the result is SKIPPED rather than PASS
        assert result.status == "SKIPPED"
        tcp_ping.assert_not_called()
        assert not any(c.name == "http_probe" for c in result.checks)

    def test_deep_skips_connectivity_when_local_config_omits_listeners(self) -> None:
        # GIVEN 'listeners' is optional in the interface's IstioIngressRouteConfig and
        # defaults to an empty list, so its absence is a valid config with nothing to probe
        validator = _make_validator(
            VALID_HTTP_DATABAG,
            local_databag={"config": json.dumps({"model": "m"})},
        )

        with patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping:
            result = validator.validate(level="deep")

        # THEN the missing key is honoured as the schema default rather than reported as FAIL
        assert result.status == "SKIPPED"
        tcp_ping.assert_not_called()

    @pytest.mark.parametrize(
        "config",
        [
            [{"port": 8080, "protocol": "HTTP"}],  # top-level value is not an object
            {"listeners": [{"port": 8080, "protocol": "HTTP"}]},  # required 'model' missing
            {"model": 1, "listeners": []},  # 'model' is not a string
        ],
    )
    def test_deep_fails_when_local_config_violates_top_level_schema(self, config: object) -> None:
        # GIVEN a local 'config' the interface's provider would itself reject, which must
        # not reach the probes and report a PASS built on an invalid contract
        validator = _make_validator(VALID_HTTP_DATABAG, local_databag={"config": json.dumps(config)})

        with patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping:
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        tcp_ping.assert_not_called()
        connect = next(c for c in result.checks if c.name == "connect")
        assert not connect.passed

    def test_deep_fails_when_local_config_is_malformed(self) -> None:
        # GIVEN the requirer's local 'config' is not valid JSON
        validator = _make_validator(VALID_HTTP_DATABAG, local_databag={"config": "not-json"})

        with patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping:
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        tcp_ping.assert_not_called()
        connect = next(c for c in result.checks if c.name == "connect")
        assert not connect.passed

    def test_deep_probes_every_declared_http_listener_port(self) -> None:
        # GIVEN the requirer declares two HTTP listeners, and only the second is
        # actually reachable
        validator = _make_validator(
            VALID_HTTP_DATABAG,
            local_databag={
                "config": json.dumps(
                    {
                        "model": "m",
                        "listeners": [
                            {"port": 8080, "protocol": "HTTP"},
                            {"port": 9090, "protocol": "HTTP"},
                        ],
                    }
                )
            },
        )

        def _tcp_ping_side_effect(host: str, port: int, *args: object, **kwargs: object) -> None:
            if port == 8080:
                raise ConnectionRefusedError("refused")

        with (
            patch("validators.istio_ingress_route.validator._tcp_ping", side_effect=_tcp_ping_side_effect) as tcp_ping,
            patch(
                "validators.istio_ingress_route.validator._opener.open",
                return_value=_mock_http_response(200),
            ),
        ):
            result = validator.validate(level="deep")

        # THEN both listeners are probed, and the unreachable one fails the overall
        # result even though the other listener is fine
        assert result.status == "FAIL"
        assert tcp_ping.call_count == 2
        connect_checks = [c for c in result.checks if c.name == "connect"]
        assert len(connect_checks) == 2
        assert not connect_checks[0].passed
        assert connect_checks[1].passed
        # only the reachable listener gets an HTTP probe
        assert sum(1 for c in result.checks if c.name == "http_probe") == 1

    @pytest.mark.parametrize(
        "listeners",
        [
            "not-a-list",
            [{"port": 8080}],
            [{"port": 8080, "protocol": "TCP"}],
            [{"protocol": "HTTP"}],
            [{"port": "8080", "protocol": "HTTP"}],
            [{"port": 0, "protocol": "HTTP"}],
            [{"port": 70000, "protocol": "HTTP"}],
            [{"port": True, "protocol": "HTTP"}],
            [{"port": 8080, "protocol": "http"}],  # lowercase: not a wire value the provider can emit
            [{"port": 9090, "protocol": "Grpc"}],  # mixed case: same reasoning
            "bad-listeners",
        ],
    )
    def test_deep_fails_when_listener_entry_is_malformed(self, listeners: object) -> None:
        # GIVEN a local 'config' whose 'listeners' entries don't match the interface's
        # schema (not a list, missing required fields, wrong types, or an out-of-range
        # port). A validator that silently filtered these out (rather than failing)
        # would misreport a genuinely broken local contract as "nothing to probe" and
        # let deep validation fall back to a passing simple-only result.
        validator = _make_validator(
            VALID_HTTP_DATABAG,
            local_databag={"config": json.dumps({"model": "m", "listeners": listeners})},
        )

        with patch("validators.istio_ingress_route.validator._tcp_ping") as tcp_ping:
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        tcp_ping.assert_not_called()
        connect = next(c for c in result.checks if c.name == "connect")
        assert not connect.passed


# ---------------------------------------------------------------------------
# Tests: HTTPS deep probes with a private/self-signed CA
# ---------------------------------------------------------------------------


class TestIstioIngressRouteValidatorHttpsCertVerification:
    def test_build_opener_skips_cert_verification_for_https(self) -> None:
        # GIVEN this relation carries no CA/trust info (only external_host/tls_enabled),
        # so there is no way for this validator to verify Istio's own private CA
        opener = _build_opener()

        # THEN an HTTPSHandler with certificate verification disabled is wired in,
        # rather than relying on urllib's default (verifying) HTTPS handling
        https_handlers = [h for h in opener.handlers if isinstance(h, HTTPSHandler)]  # type: ignore[attr-defined]
        assert https_handlers
        context = https_handlers[0]._context  # type: ignore[attr-defined] # only way to introspect the wired-in context
        assert context.verify_mode == ssl.CERT_NONE
        assert context.check_hostname is False

    def test_http_probe_reaches_real_self_signed_https_endpoint(self) -> None:
        # End-to-end regression test using a real TLS socket with a self-signed cert,
        # reproducing the exact failure this fix addresses:
        # "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate".
        server, port, thread = _start_self_signed_https_server()
        try:
            validator = _make_validator({"external_host": f"127.0.0.1:{port}", "tls_enabled": "True"})

            # WHEN
            with patch("validators.istio_ingress_route.validator._tcp_ping"):
                result = validator.validate(level="deep")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        # THEN
        assert result.status == "PASS"
        http_check = next(c for c in result.checks if c.name == "http_probe")
        assert http_check.passed, http_check.message


def _start_self_signed_https_server(
    response_body: bytes = b"OK",
) -> tuple[HTTPServer, int, threading.Thread]:
    """Start a background HTTPS server on 127.0.0.1 backed by a self-signed cert."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required BaseHTTPRequestHandler signature
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, *args: object) -> None:  # silence default request logging
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "selfsigned")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(minutes=5))
        .sign(key, hashes.SHA256())
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = f"{tmpdir}/cert.pem"
        key_path = f"{tmpdir}/key.pem"
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(key_path, "wb") as f:
            f.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                )
            )

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(cert_path, key_path)
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)

    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread
