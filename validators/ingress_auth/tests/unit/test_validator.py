# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message
from http.client import BadStatusLine
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import ops
import pytest
import yaml

from validators.ingress_auth.validator import IngressAuthValidator, _negotiated_version
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
    UnitStub,
)

_MODULE = "validators.ingress_auth.validator"

VALID_PAYLOAD: dict[str, Any] = {
    "service": "oidc-gatekeeper",
    "port": 8080,
    "allowed-request-headers": ["cookie", "X-Auth-Token"],
    "allowed-response-headers": ["kubeflow-userid"],
}


def _nested_databag(payload: dict[str, Any] | None = None, versions: Any = ("v1",)) -> dict[str, str]:
    databag = {}
    if versions is not None:
        databag["_supported_versions"] = yaml.safe_dump(versions)
    if payload is not None:
        databag["data"] = yaml.safe_dump(payload)
    return databag


def _make_validator(
    app_databag: dict[str, str],
    endpoint: str = "ingress-auth",
    role: RelationRoleStub = RelationRoleStub.provides,
    remote_app: bool = True,
) -> IngressAuthValidator:
    app = ApplicationStub() if remote_app else None
    data: dict[ApplicationStub | UnitStub | None, dict[str, str]] = {app: app_databag} if app else {}
    relation = RelationStub(name=endpoint, id=0, app=app, data=data)
    charm = make_charm_from_relation(relation, role=role, interface_name="ingress-auth")
    return IngressAuthValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def _addrinfo(*addresses: str) -> list[tuple[Any, ...]]:
    """Shape a socket.getaddrinfo() return value for the given addresses."""
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 0),
        )
        for address in addresses
    ]


@contextmanager
def _reachable() -> Iterator[MagicMock]:
    """Make the reachability checks succeed without touching the network."""
    with (
        patch(f"{_MODULE}.socket.getaddrinfo", return_value=_addrinfo("10.152.183.29")) as resolve,
        patch(f"{_MODULE}.socket.create_connection"),
    ):
        yield resolve


def _headers(**values: str) -> Message:
    message = Message()
    for key, value in values.items():
        message[key.replace("_", "-")] = value
    return message


def _mock_response(status: int, headers: Message | None = None) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.status = status
    response.headers = headers if headers is not None else Message()
    return response


def _mock_opener(response: Any) -> MagicMock:
    opener = MagicMock()
    if isinstance(response, Exception):
        opener.open.side_effect = response
    else:
        opener.open.return_value = response
    return opener


def _checks_by_name(result: Any) -> dict[str, Any]:
    return {check.name: check for check in result.checks}


def _set_models(validator: IngressAuthValidator, local_uuid: str, remote_uuid: str) -> None:
    """Give the stubs the model identity that cross-model detection relies on."""
    cast(Any, validator.charm).model.uuid = local_uuid
    cast(Any, validator.relation).remote_model = SimpleNamespace(uuid=remote_uuid)


class _RaisingRemoteModel:
    """Stands in for Juju versions that do not implement 'relation-model-get'."""

    @property
    def uuid(self) -> str:
        raise ops.ModelError("ERROR unknown command: relation-model-get")


# ---------------------------------------------------------------------------
# Role and level gating
# ---------------------------------------------------------------------------


class TestGating:
    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.provides, False),
            (RelationRoleStub.requires, True),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_skipped_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN a relation held in a given role
        validator = _make_validator(_nested_databag(VALID_PAYLOAD), role=role)

        # WHEN the validator runs at the simple level
        result = validator.validate("simple")

        # THEN only the provider side is validated
        assert (result.status == "SKIPPED") is should_skip

    def test_unsupported_level_is_skipped(self) -> None:
        # GIVEN a healthy provider-side relation
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))

        # WHEN the validator runs at an unsupported level
        result = validator.validate("uat")

        # THEN it reports SKIPPED rather than a false pass
        assert result.status == "SKIPPED"
        assert result.error is not None
        assert "uat" in result.error

    def test_missing_remote_app_is_error(self) -> None:
        # GIVEN a relation with no remote application in scope
        validator = _make_validator({}, remote_app=False)

        # WHEN the validator runs
        result = validator.validate("simple")

        # THEN it reports ERROR
        assert result.status == "ERROR"


# ---------------------------------------------------------------------------
# SDI version negotiation
# ---------------------------------------------------------------------------


class TestVersionNegotiation:
    @pytest.mark.parametrize(
        ("usable", "expected"),
        [
            (["v1"], "v1"),
            (["v1", "v2"], "v2"),
            (["v2", "v1"], "v2"),
            (["v9", "v10"], "v10"),
            (["v10", "v9"], "v10"),
        ],
    )
    def test_highest_common_version_wins(self, usable: list[str], expected: str) -> None:
        # GIVEN versions both sides support; SDI parses 'vN' to the integer N and takes
        # max() of the intersection, so selection is numeric and order-independent
        assert _negotiated_version(usable) == expected


# ---------------------------------------------------------------------------
# Simple level
# ---------------------------------------------------------------------------


class TestSimpleLevel:
    def test_valid_nested_payload_passes(self) -> None:
        # GIVEN a requirer that published a well-formed nested SDI payload
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))

        # WHEN the validator runs at the simple level
        with _reachable():
            result = validator.validate("simple")

        # THEN the wire-format and reachability checks pass, without probing for a decision
        assert result.status == "PASS"
        assert set(_checks_by_name(result)) == {
            "supported_versions",
            "payload",
            "schema",
            "field_types",
            "auth_service_dns",
            "auth_service_connect",
        }

    def test_flat_payload_is_rejected(self) -> None:
        # GIVEN a requirer that serialised each field into its own databag key; the
        # ingress-auth schema does not set SDI's `flat: true`, so SDI unwraps this as an
        # empty payload and istio-pilot removes the EnvoyFilter instead of programming it
        databag = {"_supported_versions": yaml.safe_dump(["v1"])}
        databag.update({key: yaml.safe_dump(value) for key, value in VALID_PAYLOAD.items()})
        validator = _make_validator(databag)

        # WHEN the validator runs at the simple level
        with _reachable():
            result = validator.validate("simple")

        # THEN the payload check fails rather than reporting a false PASS
        assert result.status == "FAIL"
        checks = _checks_by_name(result)
        assert checks["payload"].passed is False
        assert "'data'" in checks["payload"].message
        # AND no downstream check runs on a payload the provider will never see
        assert "schema" not in checks
        assert "auth_service_dns" not in checks

    def test_databag_without_data_key_fails(self) -> None:
        # GIVEN a remote that completed the version handshake but published no payload
        validator = _make_validator(_nested_databag(payload=None))

        # WHEN the validator runs at the simple level
        result = validator.validate("simple")

        # THEN the missing 'data' key is reported as a payload failure
        assert result.status == "FAIL"
        assert _checks_by_name(result)["payload"].passed is False

    @pytest.mark.parametrize("versions", [None, [], "not-a-list"])
    def test_broken_version_handshake_fails(self, versions: Any) -> None:
        # GIVEN a remote that did not complete the SDI version handshake
        validator = _make_validator(_nested_databag(VALID_PAYLOAD, versions=versions))

        # WHEN the validator runs
        result = validator.validate("simple")

        # THEN the handshake check fails and later checks are not attempted
        assert result.status == "FAIL"
        assert _checks_by_name(result)["supported_versions"].passed is False

    @pytest.mark.parametrize("versions", [["v2"], ["v2", "v3"]])
    def test_unsupported_version_is_rejected(self, versions: list[str]) -> None:
        # GIVEN a remote advertising only versions this validator cannot read, with a
        # payload that happens to satisfy the v1 schema
        validator = _make_validator(_nested_databag(VALID_PAYLOAD, versions=versions))

        # WHEN the validator runs
        result = validator.validate("simple")

        # THEN the handshake is rejected rather than the payload being read as v1
        assert result.status == "FAIL"
        versions_check = _checks_by_name(result)["supported_versions"]
        assert versions_check.passed is False
        assert "v1" in versions_check.message
        assert "payload" not in _checks_by_name(result)

    def test_superset_of_supported_versions_is_accepted(self) -> None:
        # GIVEN a remote advertising v1 alongside a newer version; SDI negotiates the
        # highest version both sides share, and the provider only supports v1
        validator = _make_validator(_nested_databag(VALID_PAYLOAD, versions=["v1", "v2"]))

        # WHEN the validator runs
        with _reachable():
            result = validator.validate("simple")

        # THEN validation proceeds against v1
        assert result.status == "PASS"
        assert "validating as v1" in _checks_by_name(result)["supported_versions"].message

    def test_malformed_version_entry_is_rejected(self) -> None:
        # GIVEN a remote advertising a usable version alongside a malformed one; SDI's
        # _parse_versions() raises InvalidSchemaVersionError over the whole list before
        # intersecting, and istio-pilot catches only the no-versions and
        # incompatible-versions errors, so the provider never reconciles
        validator = _make_validator(_nested_databag(VALID_PAYLOAD, versions=["v1", "bogus"]))

        # WHEN the validator runs
        with _reachable():
            result = validator.validate("simple")

        # THEN the handshake fails rather than passing on the usable entry alone
        assert result.status == "FAIL"
        checks = _checks_by_name(result)
        assert checks["supported_versions"].passed is False
        assert "bogus" in checks["supported_versions"].message
        assert "payload" not in checks

    def test_sdi_integer_version_form_is_accepted(self) -> None:
        # GIVEN a remote advertising the bare-integer form SDI also accepts, which
        # parses to the same version number as the local 'v1'
        validator = _make_validator(_nested_databag(VALID_PAYLOAD, versions=[1]))

        # WHEN the validator runs
        with _reachable():
            result = validator.validate("simple")

        # THEN it is treated as compatible instead of failing as a non-'vN' string
        assert result.status == "PASS"
        assert "validating as v1" in _checks_by_name(result)["supported_versions"].message

    def test_missing_payload_fails(self) -> None:
        # GIVEN a remote that completed the handshake but published no data
        validator = _make_validator(_nested_databag(payload=None))

        # WHEN the validator runs
        result = validator.validate("simple")

        # THEN the payload check fails
        assert result.status == "FAIL"
        assert _checks_by_name(result)["payload"].passed is False

    def test_undecodable_payload_fails(self) -> None:
        # GIVEN a data key that is not a YAML mapping
        databag = {"_supported_versions": yaml.safe_dump(["v1"]), "data": "- just\n- a list\n"}
        validator = _make_validator(databag)

        # WHEN the validator runs
        result = validator.validate("simple")

        # THEN the payload check fails
        assert result.status == "FAIL"
        assert "mapping" in _checks_by_name(result)["payload"].message

    @pytest.mark.parametrize(
        "data",
        [
            "8080: oidc-gatekeeper\nservice: authsvc\n",
            "true: x\n",
            "null: x\n",
        ],
    )
    def test_non_string_payload_keys_fail(self, data: str) -> None:
        # GIVEN a data mapping that YAML decodes to non-string keys, which no SDI field
        # name can produce and which would otherwise break sorting of the field list
        databag = {"_supported_versions": yaml.safe_dump(["v1"]), "data": data}
        validator = _make_validator(databag)

        # WHEN the validator runs
        result = validator.validate("simple")

        # THEN the payload is rejected before any field is read
        assert result.status == "FAIL"
        payload = _checks_by_name(result)["payload"]
        assert payload.passed is False
        assert "keys must all be strings" in payload.message
        assert "schema" not in _checks_by_name(result)

    def test_missing_required_fields_fails(self) -> None:
        # GIVEN a payload without the mandatory service field
        validator = _make_validator(_nested_databag({"port": 8080}))

        # WHEN the validator runs
        result = validator.validate("simple")

        # THEN the schema check reports the missing field
        assert result.status == "FAIL"
        assert "service" in _checks_by_name(result)["schema"].message

    @pytest.mark.parametrize(
        "overrides",
        [
            {"port": "8080"},
            {"port": 0},
            {"port": 70000},
            {"port": True},
            {"service": 42},
            {"allowed-request-headers": "cookie"},
            {"allowed-response-headers": [1, 2]},
        ],
    )
    def test_wrong_field_types_fail(self, overrides: dict[str, Any]) -> None:
        # GIVEN a payload that violates the ingress-auth v1 schema types
        validator = _make_validator(_nested_databag({**VALID_PAYLOAD, **overrides}))

        # WHEN the validator runs
        result = validator.validate("simple")

        # THEN the type check rejects it
        assert result.status == "FAIL"
        field_types = _checks_by_name(result).get("field_types")
        assert field_types is None or field_types.passed is False

    def test_optional_headers_may_be_absent(self) -> None:
        # GIVEN a payload carrying only the mandatory fields
        validator = _make_validator(_nested_databag({"service": "authsvc", "port": 8080}))

        # WHEN the validator runs
        with _reachable():
            result = validator.validate("simple")

        # THEN the optional header lists are not required
        assert result.status == "PASS"

    def test_unreachable_service_fails_at_simple(self) -> None:
        # GIVEN a schema-valid payload whose service refuses connections (workload down)
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))

        # WHEN the validator runs at the simple level
        with (
            patch(f"{_MODULE}.socket.getaddrinfo", return_value=_addrinfo("10.152.183.29")),
            patch(f"{_MODULE}.socket.create_connection", side_effect=OSError("Connection refused")),
            patch(f"{_MODULE}.build_opener") as opener,
        ):
            result = validator.validate("simple")

        # THEN simple reports the unreachable service rather than passing on schema alone
        assert result.status == "FAIL"
        checks = _checks_by_name(result)
        assert checks["field_types"].passed is True
        assert checks["auth_service_connect"].passed is False
        opener.assert_not_called()

    def test_unresolvable_service_fails_at_simple(self) -> None:
        # GIVEN a schema-valid payload whose service name does not resolve
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))

        # WHEN the validator runs at the simple level
        with patch(f"{_MODULE}.socket.getaddrinfo", side_effect=OSError("Name or service not known")):
            result = validator.validate("simple")

        # THEN resolution is reported and no connection is attempted
        assert result.status == "FAIL"
        checks = _checks_by_name(result)
        assert checks["auth_service_dns"].passed is False
        assert "auth_service_connect" not in checks

    def test_simple_does_not_ask_for_an_authorization_decision(self) -> None:
        # GIVEN a fully healthy authorization service
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))

        # WHEN the validator runs at the simple level
        with _reachable(), patch(f"{_MODULE}.build_opener") as opener:
            result = validator.validate("simple")

        # THEN the decision probe stays exclusive to the deep level
        assert result.status == "PASS"
        checks = _checks_by_name(result)
        assert "ext_authz_probe" not in checks
        assert "auth_decision" not in checks
        opener.assert_not_called()


# ---------------------------------------------------------------------------
# Deep level
# ---------------------------------------------------------------------------


class TestDeepLevel:
    def test_probe_uses_get_like_the_guarded_traffic(self) -> None:
        # GIVEN an authorization service; Envoy's HTTP ext_authz client copies the
        # downstream request's :method into the check request rather than issuing a fixed
        # POST, and the traffic this interface guards is browser navigation, i.e. GET
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        error = HTTPError("http://authsvc:8080/", 302, "Found", _headers(Location="/dex/auth"), None)
        opener = _mock_opener(error)

        # WHEN the validator runs at the deep level
        with _reachable(), patch(f"{_MODULE}.build_opener", return_value=opener):
            validator.validate("deep")

        # THEN the probe is a bodyless GET
        request = opener.open.call_args.args[0]
        assert request.get_method() == "GET"
        assert request.data is None

    def test_redirect_decision_passes(self) -> None:
        # GIVEN an authorization service that redirects unauthenticated requests to a login page
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        error = HTTPError("http://authsvc:8080/", 302, "Found", _headers(Location="/dex/auth"), None)

        # WHEN the validator runs at the deep level
        with _reachable(), patch(f"{_MODULE}.build_opener", return_value=_mock_opener(error)):
            result = validator.validate("deep")

        # THEN the redirect is recognised as an actionable DENY decision
        assert result.status == "PASS"
        checks = _checks_by_name(result)
        assert checks["auth_service_dns"].passed is True
        assert checks["auth_service_connect"].passed is True
        assert "DENY (302)" in checks["auth_decision"].message

    def test_allow_decision_reports_upstream_headers(self) -> None:
        # GIVEN an authorization service that allows the request and injects an identity header
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        response = _mock_response(200, _headers(kubeflow_userid="user@example.com"))

        # WHEN the validator runs at the deep level
        with _reachable(), patch(f"{_MODULE}.build_opener", return_value=_mock_opener(response)):
            result = validator.validate("deep")

        # THEN the ALLOW decision is reported with the declared upstream headers
        assert result.status == "PASS"
        assert "kubeflow-userid" in _checks_by_name(result)["auth_decision"].message

    @pytest.mark.parametrize("status", [201, 202, 204])
    def test_non_200_success_is_not_an_allow(self, status: int) -> None:
        # GIVEN a service that signals success with a 2xx other than 200, as proxies
        # accepting any 2xx would permit
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        response = _mock_response(status, _headers(kubeflow_userid="user@example.com"))

        # WHEN the validator runs at the deep level
        with _reachable(), patch(f"{_MODULE}.build_opener", return_value=_mock_opener(response)):
            result = validator.validate("deep")

        # THEN it is reported as a deny, because ext_authz admits traffic only on 200
        assert result.status == "FAIL"
        message = _checks_by_name(result)["auth_decision"].message
        assert "ALLOW" not in message
        assert "only on 200" in message

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_redirect_without_location_fails(self, status: int) -> None:
        # GIVEN a service returning a status that must carry Location, but omitting it
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        error = HTTPError("http://authsvc:8080/", status, "Redirect", _headers(), None)

        # WHEN the validator runs at the deep level
        with _reachable(), patch(f"{_MODULE}.build_opener", return_value=_mock_opener(error)):
            result = validator.validate("deep")

        # THEN the decision is unusable by the gateway
        assert result.status == "FAIL"
        assert _checks_by_name(result)["auth_decision"].passed is False

    @pytest.mark.parametrize("status", [300, 304, 305])
    def test_non_redirect_3xx_without_location_is_a_deny(self, status: int) -> None:
        # GIVEN a 3xx status that is not a redirect and legitimately carries no Location;
        # Envoy denies on any non-200, non-5xx response, so the gateway can still act on it
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        error = HTTPError("http://authsvc:8080/", status, "Not a redirect", _headers(), None)

        # WHEN the validator runs at the deep level
        with _reachable(), patch(f"{_MODULE}.build_opener", return_value=_mock_opener(error)):
            result = validator.validate("deep")

        # THEN it is reported as a DENY rather than a missing-Location failure
        assert result.status == "PASS"
        assert f"DENY ({status})" in _checks_by_name(result)["auth_decision"].message

    def test_deny_decision_passes(self) -> None:
        # GIVEN an authorization service that denies with 401
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        error = HTTPError("http://authsvc:8080/", 401, "Unauthorized", _headers(), None)

        # WHEN the validator runs at the deep level
        with _reachable(), patch(f"{_MODULE}.build_opener", return_value=_mock_opener(error)):
            result = validator.validate("deep")

        # THEN an explicit deny is a valid decision
        assert result.status == "PASS"
        assert "DENY (401)" in _checks_by_name(result)["auth_decision"].message

    def test_server_error_fails(self) -> None:
        # GIVEN an authorization service that cannot produce a decision
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        error = HTTPError("http://authsvc:8080/", 503, "Unavailable", _headers(), None)

        # WHEN the validator runs at the deep level
        with _reachable(), patch(f"{_MODULE}.build_opener", return_value=_mock_opener(error)):
            result = validator.validate("deep")

        # THEN the probe fails
        assert result.status == "FAIL"
        assert _checks_by_name(result)["ext_authz_probe"].passed is False

    def test_unresolvable_service_fails(self) -> None:
        # GIVEN an advertised service name that does not resolve
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))

        # WHEN the validator runs at the deep level
        with patch(f"{_MODULE}.socket.getaddrinfo", side_effect=OSError("Name or service not known")):
            result = validator.validate("deep")

        # THEN DNS resolution is reported as the failure and no probe is attempted
        assert result.status == "FAIL"
        checks = _checks_by_name(result)
        assert checks["auth_service_dns"].passed is False
        assert "auth_service_connect" not in checks

    def test_ipv6_only_service_resolves(self) -> None:
        # GIVEN a service on an IPv6-only cluster, which publishes an AAAA record but no A
        # record; create_connection and Envoy both address it happily
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        error = HTTPError("http://authsvc:8080/", 302, "Found", _headers(Location="/dex/auth"), None)

        # WHEN the validator runs at the deep level
        with (
            patch(f"{_MODULE}.socket.getaddrinfo", return_value=_addrinfo("fd00::1")),
            patch(f"{_MODULE}.socket.create_connection"),
            patch(f"{_MODULE}.build_opener", return_value=_mock_opener(error)),
        ):
            result = validator.validate("deep")

        # THEN the address family does not affect the verdict
        assert result.status == "PASS"
        assert "fd00::1" in _checks_by_name(result)["auth_service_dns"].message

    def test_multiple_addresses_are_rendered_as_readable_text(self) -> None:
        # GIVEN a service that resolves to several addresses
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        error = HTTPError("http://authsvc:8080/", 302, "Found", _headers(Location="/dex/auth"), None)

        # WHEN the validator runs at the deep level
        with (
            patch(f"{_MODULE}.socket.getaddrinfo", return_value=_addrinfo("10.0.0.2", "10.0.0.1")),
            patch(f"{_MODULE}.socket.create_connection"),
            patch(f"{_MODULE}.build_opener", return_value=_mock_opener(error)),
        ):
            result = validator.validate("deep")

        # THEN they are joined as prose rather than dumped as a Python list repr,
        # and the trailing period never abuts an address
        message = _checks_by_name(result)["auth_service_dns"].message
        assert "10.0.0.1, 10.0.0.2" in message
        assert "[" not in message
        assert message.endswith("svc.cluster.local.")

    def test_resolution_without_addresses_fails(self) -> None:
        # GIVEN a resolver that returns no addresses rather than raising
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))

        # WHEN the validator runs at the deep level
        with patch(f"{_MODULE}.socket.getaddrinfo", return_value=[]):
            result = validator.validate("deep")

        # THEN DNS is reported as the failure instead of raising IndexError
        assert result.status == "FAIL"
        checks = _checks_by_name(result)
        assert checks["auth_service_dns"].passed is False
        assert "auth_service_connect" not in checks

    def test_unreachable_service_fails(self) -> None:
        # GIVEN an advertised service that resolves but refuses connections (workload down)
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))

        # WHEN the validator runs at the deep level
        with (
            patch(f"{_MODULE}.socket.getaddrinfo", return_value=_addrinfo("10.152.183.29")),
            patch(f"{_MODULE}.socket.create_connection", side_effect=OSError("Connection refused")),
        ):
            result = validator.validate("deep")

        # THEN connectivity is reported as the failure
        assert result.status == "FAIL"
        checks = _checks_by_name(result)
        assert checks["auth_service_connect"].passed is False
        assert "ext_authz_probe" not in checks

    def test_transport_failure_fails(self) -> None:
        # GIVEN an authorization service whose HTTP layer is broken
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))

        # WHEN the validator runs at the deep level
        with _reachable(), patch(f"{_MODULE}.build_opener", return_value=_mock_opener(URLError("timed out"))):
            result = validator.validate("deep")

        # THEN the probe reports the transport failure without guessing at a cause
        assert result.status == "FAIL"
        probe = _checks_by_name(result)["ext_authz_probe"]
        assert probe.passed is False
        assert "plaintext HTTP" not in probe.message

    @pytest.mark.parametrize(
        "reason",
        [ConnectionResetError(104, "Connection reset by peer"), BadStatusLine("\x16\x03\x01")],
    )
    def test_tls_only_service_is_diagnosed(self, reason: Exception) -> None:
        # GIVEN an authorization service that accepts only TLS on the advertised port
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))

        # WHEN the validator probes it over plaintext HTTP, as the provider does
        with _reachable(), patch(f"{_MODULE}.build_opener", return_value=_mock_opener(URLError(reason))):
            result = validator.validate("deep")

        # THEN the scheme mismatch is named rather than reported as a network fault
        assert result.status == "FAIL"
        assert "plaintext HTTP" in _checks_by_name(result)["ext_authz_probe"].message

    def test_deep_uses_in_cluster_fqdn(self) -> None:
        # GIVEN a deployment whose model name forms the in-cluster FQDN
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        error = HTTPError("http://authsvc:8080/", 302, "Found", _headers(Location="/dex/auth"), None)

        # WHEN the validator runs at the deep level
        with _reachable() as resolve, patch(f"{_MODULE}.build_opener", return_value=_mock_opener(error)):
            validator.validate("deep")

        # THEN only the address the provider programs into the proxy is resolved
        assert [call.args[0] for call in resolve.call_args_list] == ["oidc-gatekeeper.test-model.svc.cluster.local"]

    def test_deep_skips_probing_when_schema_is_broken(self) -> None:
        # GIVEN a payload that fails the simple-level checks
        validator = _make_validator(_nested_databag({"port": 8080}))

        # WHEN the validator runs at the deep level
        with patch(f"{_MODULE}.socket.getaddrinfo") as resolve:
            result = validator.validate("deep")

        # THEN no network activity is attempted
        assert result.status == "FAIL"
        resolve.assert_not_called()


# ---------------------------------------------------------------------------
# Cross-model relations
# ---------------------------------------------------------------------------


class TestCrossModel:
    def test_unresolvable_cross_model_service_explains_the_cause(self) -> None:
        # GIVEN a requirer in another model, whose service the provider cannot address
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        _set_models(validator, local_uuid="uuid-a", remote_uuid="uuid-b")

        # WHEN the validator runs at the deep level
        with patch(f"{_MODULE}.socket.getaddrinfo", side_effect=OSError("Name or service not known")):
            result = validator.validate("deep")

        # THEN the failure names the cross-model cause rather than blaming DNS
        assert result.status == "FAIL"
        message = _checks_by_name(result)["auth_service_dns"].message
        assert "cross-model" in message
        assert "oidc-gatekeeper.test-model.svc.cluster.local" in message

    def test_bridged_cross_model_service_passes_but_is_flagged(self) -> None:
        # GIVEN a cross-model relation bridged by a local alias for the remote service
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        _set_models(validator, local_uuid="uuid-a", remote_uuid="uuid-b")
        error = HTTPError("http://authsvc:8080/", 302, "Found", _headers(Location="/dex/auth"), None)

        # WHEN the validator runs at the deep level
        with _reachable(), patch(f"{_MODULE}.build_opener", return_value=_mock_opener(error)):
            result = validator.validate("deep")

        # THEN it passes, because the authorization path genuinely works
        assert result.status == "PASS"
        assert "local alias" in _checks_by_name(result)["auth_service_dns"].message

    def test_same_model_relation_is_not_flagged(self) -> None:
        # GIVEN a requirer in the same model as the provider
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        _set_models(validator, local_uuid="uuid-a", remote_uuid="uuid-a")

        # WHEN the validator runs at the deep level and resolution fails
        with patch(f"{_MODULE}.socket.getaddrinfo", side_effect=OSError("Name or service not known")):
            result = validator.validate("deep")

        # THEN no cross-model explanation is attached
        assert "cross-model" not in _checks_by_name(result)["auth_service_dns"].message

    def test_old_juju_without_remote_model_still_validates(self) -> None:
        # GIVEN a Juju too old to report the remote model
        validator = _make_validator(_nested_databag(VALID_PAYLOAD))
        cast(Any, validator.charm).model.uuid = "uuid-a"
        cast(Any, validator.relation).remote_model = _RaisingRemoteModel()

        # WHEN the validator runs at the deep level
        with patch(f"{_MODULE}.socket.getaddrinfo", side_effect=OSError("Name or service not known")):
            result = validator.validate("deep")

        # THEN the topology is simply unknown and validation still reports the failure
        assert result.status == "FAIL"
        assert "cross-model" not in _checks_by_name(result)["auth_service_dns"].message
