# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the cross_model_mesh interface validator.

Coverage targets
----------------
* L1 (simple) — requires role: >=1 valid + >=2 invalid scenarios.
* L1 (simple) — provides role: >=1 valid + >=2 invalid scenarios.
* L2 (deep)   — provides role: >=1 valid + >=2 invalid scenarios.
"""

import dataclasses
import json
import time
import urllib.request
from typing import Any, cast
from unittest.mock import MagicMock, mock_open, patch

import ops
import pytest

from validators.cross_model_mesh.validator import (
    CMRData,
    CrossModelMeshValidator,
    _check_dns_reachable,
    _check_identity_format,
    _check_mesh_data_plane_reachable,
    _connect_to_resolved_address,
    _decode_cmr_data,
    _discover_service_ports,
)
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_LOCAL_APP_NAME = "catalogue-k8s"
_LOCAL_MODEL_NAME = "cross-model-mesh-test"

_VALID_CMR_DATA = CMRData(app_name=_LOCAL_APP_NAME, juju_model_name=_LOCAL_MODEL_NAME)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_requires_validator(
    local_databag: dict[str, str],
    endpoint: str = "require-cmr-mesh",
    local_app_name: str = _LOCAL_APP_NAME,
    local_model_name: str = _LOCAL_MODEL_NAME,
) -> CrossModelMeshValidator:
    """Build a validator on the requirer side. Remote app publishes nothing."""
    remote_app = ApplicationStub(name="istio-beacon-k8s")
    relation = RelationStub(name=endpoint, id=0, app=remote_app, data={remote_app: {}})
    stub_charm = make_charm_from_relation(
        relation,
        interface_name="cross_model_mesh",
        role=RelationRoleStub.requires,
        local_app_name=local_app_name,
        local_model_name=local_model_name,
    )
    # The requirer publishes cmr_data into its own local app databag.
    relation.data[stub_charm.app] = local_databag
    charm = cast(ops.CharmBase, stub_charm)
    return CrossModelMeshValidator(charm, cast(ops.Relation, relation))


def _make_provides_validator(
    remote_databag: dict[str, str],
    endpoint: str = "provide-cmr-mesh",
    remote_app_name: str = _LOCAL_APP_NAME,
) -> CrossModelMeshValidator:
    """Build a validator on the provider side. Remote app is the requirer."""
    remote_app = ApplicationStub(name=remote_app_name)
    relation = RelationStub(name=endpoint, id=0, app=remote_app, data={remote_app: remote_databag})
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(
            relation,
            interface_name="cross_model_mesh",
            role=RelationRoleStub.provides,
            local_app_name="istio-beacon-k8s",
            local_model_name=_LOCAL_MODEL_NAME,
        ),
    )
    return CrossModelMeshValidator(charm, cast(ops.Relation, relation))


def _make_validator_no_remote_app(
    endpoint: str = "provide-cmr-mesh",
    role: RelationRoleStub = RelationRoleStub.provides,
) -> CrossModelMeshValidator:
    """Produce a validator where the remote app is absent (relation.app is None)."""
    relation = RelationStub(name=endpoint, id=0, app=None, data={})
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(relation, interface_name="cross_model_mesh", role=role),
    )
    return CrossModelMeshValidator(charm, cast(ops.Relation, relation))


# ---------------------------------------------------------------------------
# Pure-helper unit tests (no charm context needed)
# ---------------------------------------------------------------------------


class TestDecodeCmrData:
    def test_valid_data(self) -> None:
        cmr_data, check = _decode_cmr_data(json.dumps(dataclasses.asdict(_VALID_CMR_DATA)), source="test")
        assert check.passed, check.message
        assert cmr_data == _VALID_CMR_DATA

    def test_missing_field(self) -> None:
        cmr_data, check = _decode_cmr_data(json.dumps({"app_name": "foo"}), source="test")
        assert not check.passed
        assert cmr_data is None
        assert "juju_model_name" in check.message

    def test_empty_string(self) -> None:
        cmr_data, check = _decode_cmr_data("", source="test")
        assert not check.passed
        assert cmr_data is None
        assert "missing" in check.message.lower()

    def test_invalid_json(self) -> None:
        cmr_data, check = _decode_cmr_data("{not json", source="test")
        assert not check.passed
        assert cmr_data is None
        assert "json" in check.message.lower()

    def test_not_an_object(self) -> None:
        cmr_data, check = _decode_cmr_data(json.dumps(["a", "b"]), source="test")
        assert not check.passed
        assert cmr_data is None

    def test_non_string_field(self) -> None:
        cmr_data, check = _decode_cmr_data(json.dumps({"app_name": 1, "juju_model_name": "m"}), source="test")
        assert not check.passed
        assert cmr_data is None


class TestCheckIdentityFormat:
    def test_valid_names(self) -> None:
        check = _check_identity_format(CMRData(app_name="catalogue-k8s", juju_model_name="cross-model-mesh-test"))
        assert check.passed, check.message

    def test_invalid_app_name_uppercase(self) -> None:
        check = _check_identity_format(CMRData(app_name="Catalogue-K8s", juju_model_name="testing"))
        assert not check.passed
        assert "app_name" in check.message

    def test_invalid_model_name_starts_with_hyphen(self) -> None:
        check = _check_identity_format(CMRData(app_name="catalogue-k8s", juju_model_name="-testing"))
        assert not check.passed
        assert "juju_model_name" in check.message

    def test_invalid_empty_app_name(self) -> None:
        check = _check_identity_format(CMRData(app_name="", juju_model_name="testing"))
        assert not check.passed

    def test_invalid_trailing_newline(self) -> None:
        """`.match()` alone would let `$` match just before a trailing newline; fullmatch must not."""
        check = _check_identity_format(CMRData(app_name="catalogue-k8s\n", juju_model_name="testing"))
        assert not check.passed
        assert "app_name" in check.message


class TestCheckDnsReachable:
    def test_resolves(self) -> None:
        with patch(
            "validators.cross_model_mesh.validator.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 0))]
        ):
            check, addrs = _check_dns_reachable(_VALID_CMR_DATA)
        assert check.passed, check.message
        assert addrs == [(2, 1, 6, "", ("10.0.0.1", 0))]

    def test_resolution_failure(self) -> None:
        with patch("validators.cross_model_mesh.validator.socket.getaddrinfo", side_effect=OSError("not found")):
            check, addrs = _check_dns_reachable(_VALID_CMR_DATA)
        assert not check.passed
        assert "not found" in check.message
        assert addrs is None

    def test_resolution_timeout(self) -> None:
        """socket.setdefaulttimeout() does not bound getaddrinfo(); a hung resolver call must
        still be treated as a failure within _DNS_TIMEOUT rather than blocking indefinitely."""

        def _hangs_forever(*_args: Any, **_kwargs: Any) -> Any:
            time.sleep(3600)

        with (
            patch("validators.cross_model_mesh.validator.socket.getaddrinfo", side_effect=_hangs_forever),
            patch("validators.cross_model_mesh.validator._DNS_TIMEOUT", 0.05),
        ):
            check, addrs = _check_dns_reachable(_VALID_CMR_DATA)
        assert not check.passed
        assert "did not complete" in check.message
        assert addrs is None


class TestConnectToResolvedAddress:
    """Unit tests for the low-level socket helper used by the mesh canary.

    These verify it connects using the already-resolved addresses directly
    (never invoking hostname resolution itself), tries every address on
    failure, and raises the last error when none succeed.
    """

    def test_connects_using_first_address(self) -> None:
        resolved_addrs = [(2, 1, 6, "", ("10.0.0.5", 0))]
        fake_socket = MagicMock()

        with patch("validators.cross_model_mesh.validator.socket.socket", return_value=fake_socket) as mock_socket:
            sock = _connect_to_resolved_address(resolved_addrs, 80, 5)

        mock_socket.assert_called_once_with(2, 1, 6)
        fake_socket.settimeout.assert_called_once_with(5)
        fake_socket.connect.assert_called_once_with(("10.0.0.5", 80))
        assert sock is fake_socket

    def test_falls_back_to_second_address_when_first_fails(self) -> None:
        resolved_addrs = [(2, 1, 6, "", ("10.0.0.5", 0)), (2, 1, 6, "", ("10.0.0.6", 0))]
        failing_socket = MagicMock()
        failing_socket.connect.side_effect = OSError("Connection refused")
        working_socket = MagicMock()

        with patch("validators.cross_model_mesh.validator.socket.socket", side_effect=[failing_socket, working_socket]):
            sock = _connect_to_resolved_address(resolved_addrs, 80, 5)

        failing_socket.close.assert_called_once()
        assert sock is working_socket

    def test_raises_last_error_when_all_addresses_fail(self) -> None:
        resolved_addrs = [(2, 1, 6, "", ("10.0.0.5", 0))]
        failing_socket = MagicMock()
        failing_socket.connect.side_effect = OSError("Connection refused")

        with patch("validators.cross_model_mesh.validator.socket.socket", return_value=failing_socket):
            with pytest.raises(OSError, match="Connection refused"):
                _connect_to_resolved_address(resolved_addrs, 80, 5)

    def test_deduplicates_identical_addresses(self) -> None:
        """Multiple getaddrinfo entries for the same (family, ip) should only be tried once."""
        resolved_addrs = [(2, 1, 6, "", ("10.0.0.5", 0)), (2, 2, 17, "", ("10.0.0.5", 0))]
        fake_socket = MagicMock()

        with patch("validators.cross_model_mesh.validator.socket.socket", return_value=fake_socket) as mock_socket:
            _connect_to_resolved_address(resolved_addrs, 80, 5)

        mock_socket.assert_called_once()


class TestCheckMeshDataPlaneReachable:
    _RESOLVED_ADDRS = [(2, 1, 6, "", ("10.0.0.5", 0))]

    def test_connects_using_discovered_port(self) -> None:
        with (
            patch(
                "validators.cross_model_mesh.validator._discover_service_ports",
                return_value=[80],
            ),
            patch("validators.cross_model_mesh.validator._connect_to_resolved_address") as mock_connect,
        ):
            mock_connect.return_value = MagicMock()
            check = _check_mesh_data_plane_reachable(_VALID_CMR_DATA, self._RESOLVED_ADDRS)
        assert check.passed, check.message
        assert mock_connect.call_args[0][0] == self._RESOLVED_ADDRS
        assert mock_connect.call_args[0][1] == 80
        assert "discovered via in-cluster Service lookup" in check.message

    def test_connects_using_second_port_when_first_fails(self) -> None:
        """Service port order is not a reliability signal: try every declared port."""
        attempts: list[int] = []

        def fake_connect(resolved_addrs: list[tuple[Any, ...]], port: int, timeout: float) -> Any:
            attempts.append(port)
            if port == 8080:
                raise OSError("Connection refused")
            return MagicMock()

        with (
            patch("validators.cross_model_mesh.validator._discover_service_ports", return_value=[8080, 80]),
            patch(
                "validators.cross_model_mesh.validator._connect_to_resolved_address",
                side_effect=fake_connect,
            ),
        ):
            check = _check_mesh_data_plane_reachable(_VALID_CMR_DATA, self._RESOLVED_ADDRS)
        assert check.passed, check.message
        assert attempts == [8080, 80]

    def test_connects_using_fallback_port_when_discovery_unavailable(self) -> None:
        with (
            patch("validators.cross_model_mesh.validator._discover_service_ports", return_value=None),
            patch("validators.cross_model_mesh.validator._connect_to_resolved_address") as mock_connect,
        ):
            mock_connect.return_value = MagicMock()
            check = _check_mesh_data_plane_reachable(_VALID_CMR_DATA, self._RESOLVED_ADDRS)
        assert check.passed, check.message
        assert mock_connect.call_args[0][1] == 15008
        assert "best-effort fallback" in check.message

    def test_invalid_service_with_no_ports_does_not_fall_back(self) -> None:
        """A Service discovered with zero declared ports is a definitive FAIL,
        not a silent fallback to the weaker HBONE probe."""
        with (
            patch("validators.cross_model_mesh.validator._discover_service_ports", return_value=[]),
            patch("validators.cross_model_mesh.validator._connect_to_resolved_address") as mock_connect,
        ):
            check = _check_mesh_data_plane_reachable(_VALID_CMR_DATA, self._RESOLVED_ADDRS)
        mock_connect.assert_not_called()
        assert not check.passed
        assert "no ports" in check.message

    def test_connection_refused(self) -> None:
        with (
            patch("validators.cross_model_mesh.validator._discover_service_ports", return_value=[80]),
            patch(
                "validators.cross_model_mesh.validator._connect_to_resolved_address",
                side_effect=OSError("Connection refused"),
            ),
        ):
            check = _check_mesh_data_plane_reachable(_VALID_CMR_DATA, self._RESOLVED_ADDRS)
        assert not check.passed
        assert "Connection refused" in check.message


class TestDiscoverServicePorts:
    def test_returns_none_when_service_account_files_absent(self) -> None:
        with patch("builtins.open", side_effect=FileNotFoundError("no such file")):
            assert _discover_service_ports("some-model", "some-app") is None

    def test_returns_ports_on_successful_api_response(self) -> None:
        response_body = json.dumps({"spec": {"ports": [{"port": 80}, {"port": 8080}]}}).encode()

        class _FakeResponse:
            def read(self) -> bytes:
                return response_body

            def __enter__(self) -> "_FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        fake_opener = MagicMock()
        fake_opener.open.return_value = _FakeResponse()

        with (
            patch("builtins.open", mock_open(read_data="fake-token")),
            patch("validators.cross_model_mesh.validator.ssl.create_default_context"),
            patch("validators.cross_model_mesh.validator.urllib.request.build_opener", return_value=fake_opener),
        ):
            ports = _discover_service_ports("some-model", "some-app")

        assert ports == [80, 8080]

    def test_returns_none_on_api_error(self) -> None:
        import urllib.error

        fake_opener = MagicMock()
        fake_opener.open.side_effect = urllib.error.URLError("unreachable")

        with (
            patch("builtins.open", mock_open(read_data="fake-token")),
            patch("validators.cross_model_mesh.validator.ssl.create_default_context"),
            patch("validators.cross_model_mesh.validator.urllib.request.build_opener", return_value=fake_opener),
        ):
            ports = _discover_service_ports("some-model", "some-app")

        assert ports is None

    def test_returns_empty_list_when_service_has_no_ports(self) -> None:
        response_body = json.dumps({"spec": {"ports": []}}).encode()

        class _FakeResponse:
            def read(self) -> bytes:
                return response_body

            def __enter__(self) -> "_FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        fake_opener = MagicMock()
        fake_opener.open.return_value = _FakeResponse()

        with (
            patch("builtins.open", mock_open(read_data="fake-token")),
            patch("validators.cross_model_mesh.validator.ssl.create_default_context"),
            patch("validators.cross_model_mesh.validator.urllib.request.build_opener", return_value=fake_opener),
        ):
            ports = _discover_service_ports("some-model", "some-app")

        assert ports == []

    def test_does_not_use_a_proxy_for_the_in_cluster_api_request(self) -> None:
        """The bearer token must never be sent to an env-configured proxy."""
        response_body = json.dumps({"spec": {"ports": [{"port": 80}]}}).encode()

        class _FakeResponse:
            def read(self) -> bytes:
                return response_body

            def __enter__(self) -> "_FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        fake_opener = MagicMock()
        fake_opener.open.return_value = _FakeResponse()

        with (
            patch("builtins.open", mock_open(read_data="fake-token")),
            patch("validators.cross_model_mesh.validator.ssl.create_default_context"),
            patch(
                "validators.cross_model_mesh.validator.urllib.request.build_opener", return_value=fake_opener
            ) as mock_build_opener,
        ):
            _discover_service_ports("some-model", "some-app")

        handlers = mock_build_opener.call_args.args
        assert any(isinstance(h, urllib.request.ProxyHandler) and getattr(h, "proxies", None) == {} for h in handlers)


# ---------------------------------------------------------------------------
# L1 — requires role
# ---------------------------------------------------------------------------


class TestRequiresSimple:
    def test_valid_self_published_data_passes(self) -> None:
        validator = _make_requires_validator({"cmr_data": json.dumps(dataclasses.asdict(_VALID_CMR_DATA))})

        result = validator.validate(level="simple")

        assert result.status == "PASS", result
        assert result.level == "simple"
        assert result.role == "requires"
        assert result.interface == "cross_model_mesh"
        schema = next(c for c in result.checks if c.name == "schema")
        assert schema.passed
        consistency = next(c for c in result.checks if c.name == "self_consistency")
        assert consistency.passed

    def test_invalid_missing_local_cmr_data(self) -> None:
        validator = _make_requires_validator({})

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed
        assert "cmr_data" in schema.message

    def test_invalid_self_inconsistent_identity(self) -> None:
        """The published identity doesn't match this charm's actual app/model."""
        wrong_data = {"app_name": "some-other-app", "juju_model_name": _LOCAL_MODEL_NAME}
        validator = _make_requires_validator({"cmr_data": json.dumps(wrong_data)})

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        consistency = next(c for c in result.checks if c.name == "self_consistency")
        assert not consistency.passed
        assert "does not match" in consistency.message

    def test_no_remote_app_returns_error(self) -> None:
        validator = _make_validator_no_remote_app(endpoint="require-cmr-mesh", role=RelationRoleStub.requires)
        result = validator.validate(level="simple")
        assert result.status == "ERROR"
        assert result.error is not None

    def test_deep_level_skipped(self) -> None:
        validator = _make_requires_validator({"cmr_data": json.dumps(dataclasses.asdict(_VALID_CMR_DATA))})
        result = validator.validate(level="deep")
        assert result.status == "SKIPPED"

    def test_uat_level_skipped(self) -> None:
        validator = _make_requires_validator({"cmr_data": json.dumps(dataclasses.asdict(_VALID_CMR_DATA))})
        result = validator.validate(level="uat")
        assert result.status == "SKIPPED"


# ---------------------------------------------------------------------------
# L1 — provides role
# ---------------------------------------------------------------------------


class TestProvidesSimple:
    def test_valid_remote_data_passes(self) -> None:
        validator = _make_provides_validator({"cmr_data": json.dumps(dataclasses.asdict(_VALID_CMR_DATA))})

        with patch(
            "validators.cross_model_mesh.validator.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 0))]
        ):
            result = validator.validate(level="simple")

        assert result.status == "PASS", result
        assert result.role == "provides"
        schema = next(c for c in result.checks if c.name == "schema")
        assert schema.passed
        fmt = next(c for c in result.checks if c.name == "identity_format")
        assert fmt.passed
        consistency = next(c for c in result.checks if c.name == "remote_app_consistency")
        assert consistency.passed
        dns = next(c for c in result.checks if c.name == "dns_reachable")
        assert dns.passed

    def test_invalid_missing_remote_cmr_data(self) -> None:
        validator = _make_provides_validator({})

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        schema = next(c for c in result.checks if c.name == "schema")
        assert not schema.passed

    def test_invalid_malformed_identity(self) -> None:
        bad_data = {"app_name": "Not_Valid!", "juju_model_name": _LOCAL_MODEL_NAME}
        validator = _make_provides_validator({"cmr_data": json.dumps(bad_data)})

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        fmt = next(c for c in result.checks if c.name == "identity_format")
        assert not fmt.passed

    def test_invalid_remote_app_impersonation(self) -> None:
        """A requirer that declares a different app_name than the actual relation
        app must fail, even if that identity would otherwise resolve."""
        impersonated_data = {"app_name": "some-other-app", "juju_model_name": _LOCAL_MODEL_NAME}
        validator = _make_provides_validator(
            {"cmr_data": json.dumps(impersonated_data)}, remote_app_name=_LOCAL_APP_NAME
        )

        with patch("validators.cross_model_mesh.validator.socket.getaddrinfo") as mock_dns:
            result = validator.validate(level="simple")

        mock_dns.assert_not_called()
        assert result.status == "FAIL"
        consistency = next(c for c in result.checks if c.name == "remote_app_consistency")
        assert not consistency.passed
        assert "some-other-app" in consistency.message

    def test_invalid_dns_unresolvable(self) -> None:
        validator = _make_provides_validator({"cmr_data": json.dumps(dataclasses.asdict(_VALID_CMR_DATA))})

        with patch(
            "validators.cross_model_mesh.validator.socket.getaddrinfo",
            side_effect=OSError("Name or service not known"),
        ):
            result = validator.validate(level="simple")

        assert result.status == "FAIL"
        dns = next(c for c in result.checks if c.name == "dns_reachable")
        assert not dns.passed

    def test_no_remote_app_returns_error(self) -> None:
        validator = _make_validator_no_remote_app()
        result = validator.validate(level="simple")
        assert result.status == "ERROR"
        assert result.error is not None


# ---------------------------------------------------------------------------
# L2 — provides role
# ---------------------------------------------------------------------------


class TestProvidesDeep:
    def test_valid_mesh_path_reachable(self) -> None:
        validator = _make_provides_validator({"cmr_data": json.dumps(dataclasses.asdict(_VALID_CMR_DATA))})

        with (
            patch(
                "validators.cross_model_mesh.validator.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("10.0.0.5", 0))],
            ),
            patch("validators.cross_model_mesh.validator._discover_service_ports", return_value=[80]),
            patch("validators.cross_model_mesh.validator._connect_to_resolved_address") as mock_connect,
        ):
            mock_connect.return_value = MagicMock()
            result = validator.validate(level="deep")

        assert result.status == "PASS", result
        assert result.level == "deep"
        canary = next(c for c in result.checks if c.name == "mesh_data_plane_reachable")
        assert canary.passed

    def test_invalid_mesh_path_unreachable(self) -> None:
        validator = _make_provides_validator({"cmr_data": json.dumps(dataclasses.asdict(_VALID_CMR_DATA))})

        with (
            patch(
                "validators.cross_model_mesh.validator.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("10.0.0.5", 0))],
            ),
            patch("validators.cross_model_mesh.validator._discover_service_ports", return_value=[80]),
            patch(
                "validators.cross_model_mesh.validator._connect_to_resolved_address",
                side_effect=OSError("Connection refused"),
            ),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        canary = next(c for c in result.checks if c.name == "mesh_data_plane_reachable")
        assert not canary.passed
        assert "Connection refused" in canary.message

    def test_invalid_schema_short_circuits_before_network(self) -> None:
        """Deep validation should not attempt any network I/O when schema is invalid."""
        validator = _make_provides_validator({})

        with patch("validators.cross_model_mesh.validator.socket.getaddrinfo") as mock_dns:
            result = validator.validate(level="deep")

        mock_dns.assert_not_called()
        assert result.status == "FAIL"

    def test_no_remote_app_returns_error(self) -> None:
        validator = _make_validator_no_remote_app()
        result = validator.validate(level="deep")
        assert result.status == "ERROR"
        assert result.error is not None
