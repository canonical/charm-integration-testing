# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import ops
import pytest
from kazoo.exceptions import BadVersionError, NoAuthError, NoNodeError  # type: ignore[import-untyped]

from validators.base import PersistenceNotApplicable, PersistenceState
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import ApplicationStub, RelationRoleStub, RelationStub, SecretStub
from validators.zookeeper.validator import ZookeeperPersistenceValidator

_DATABAG = {
    "database": "/canary",
    "endpoints": "zookeeper.example:2181",
    "secret-user": "secret://user",
    "secret-tls": "secret://tls",
}
_USER_SECRET = {"username": "canary-user", "password": "canary-password"}
_TLS_SECRET = {"tls-ca": "certificate"}


@dataclass
class _Stat:
    version: int


@dataclass
class _FakeKazooClient:
    nodes: dict[str, tuple[bytes, int]]
    started: bool = False
    closed: bool = False
    acls: dict[str, list[Any]] = field(default_factory=dict)

    def start(self, timeout: int) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def create(self, path: str, value: bytes, acl: list[Any]) -> str:
        if path in self.nodes:
            raise AssertionError(f"node already exists: {path}")
        self.nodes[path] = (value, 0)
        self.acls[path] = acl
        return path

    def get(self, path: str) -> tuple[bytes, _Stat]:
        if path not in self.nodes:
            raise NoNodeError()
        value, version = self.nodes[path]
        return value, _Stat(version)

    def set(self, path: str, value: bytes, version: int) -> _Stat:
        if path not in self.nodes:
            raise NoNodeError()
        _, current_version = self.nodes[path]
        if version != current_version:
            raise BadVersionError()
        self.nodes[path] = (value, current_version + 1)
        return _Stat(current_version + 1)

    def delete(self, path: str, recursive: bool = False) -> None:
        if path not in self.nodes:
            raise NoNodeError()
        del self.nodes[path]

    def get_children(self, path: str) -> list[str]:
        prefix = f"{path.rstrip('/')}/"
        return [
            node[len(prefix) :] for node in self.nodes if node.startswith(prefix) and "/" not in node[len(prefix) :]
        ]


def _make_validator(
    *,
    role: RelationRoleStub = RelationRoleStub.requires,
    relation_id: int = 7,
    nodes: dict[str, tuple[bytes, int]] | None = None,
) -> tuple[ZookeeperPersistenceValidator, _FakeKazooClient]:
    app = ApplicationStub()
    relation = RelationStub(name="zookeeper", id=relation_id, app=app, data={app: dict(_DATABAG)})
    charm = make_charm_from_relation(relation, interface_name="zookeeper", role=role)
    charm.model._secrets = {"secret://user": _USER_SECRET, "secret://tls": _TLS_SECRET}
    fake_client = _FakeKazooClient(nodes if nodes is not None else {})
    validator = ZookeeperPersistenceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))
    return validator, fake_client


def _use_client(validator: ZookeeperPersistenceValidator, fake_client: _FakeKazooClient) -> Any:
    from contextlib import contextmanager

    @contextmanager
    def client_context(config: dict[str, str]) -> Any:
        assert config["endpoints"] == _DATABAG["endpoints"]
        assert config["username"] == _USER_SECRET["username"]
        assert config["password"] == _USER_SECRET["password"]
        yield fake_client

    return patch.object(validator, "_client", side_effect=client_context)


@pytest.mark.parametrize("role", [RelationRoleStub.provides, RelationRoleStub.peer])
class TestRoleGating:
    def test_lifecycle_methods_are_not_applicable(self, role: RelationRoleStub) -> None:
        validator, fake_client = _make_validator(role=role)
        with _use_client(validator, fake_client):
            with pytest.raises(PersistenceNotApplicable):
                validator.prepare()
            with pytest.raises(PersistenceNotApplicable):
                validator.checkpoint(PersistenceState(id=1, ref=1, token="token"))
            with pytest.raises(PersistenceNotApplicable):
                validator.cleanup()


class TestPrepare:
    def test_creates_version_one_canary_with_random_token(self) -> None:
        validator, fake_client = _make_validator()
        with _use_client(validator, fake_client):
            state = validator.prepare()

        path = f"/canary/{validator._canary_node_name(state.id)}"
        assert state.ref == 1
        assert state.token
        assert fake_client.nodes[path] == (state.token.encode(), 1)
        assert fake_client.acls[path][0].id.scheme == "sasl"
        assert fake_client.acls[path][0].id.id == _USER_SECRET["username"]

    def test_client_uses_digest_md5_sasl_credentials(self) -> None:
        validator, fake_client = _make_validator()
        config = {
            "endpoints": _DATABAG["endpoints"],
            "username": _USER_SECRET["username"],
            "password": _USER_SECRET["password"],
        }
        with patch("validators.zookeeper.validator.KazooClient", return_value=fake_client) as client_factory:
            with validator._client(config):
                pass

        assert client_factory.call_args is not None
        assert client_factory.call_args.kwargs["sasl_options"] == {
            "mechanism": "DIGEST-MD5",
            "username": _USER_SECRET["username"],
            "password": _USER_SECRET["password"],
            "service": "zookeeper",
            "principal": "zk-sasl-md5",
        }

    @pytest.mark.parametrize("fail_during_use", [False, True], ids=["success", "error"])
    def test_tls_client_uses_temporary_ca_and_cleans_up(self, fail_during_use: bool) -> None:
        # GIVEN a TLS-enabled connection and a stubbed Kazoo client.
        validator, fake_client = _make_validator()
        config = validator._connection_config()

        # WHEN the real client context is entered and then exited.
        expected_outcome = (
            pytest.raises(RuntimeError, match="client operation failed") if fail_during_use else nullcontext()
        )
        with patch("validators.zookeeper.validator.KazooClient", return_value=fake_client) as client_factory:
            with expected_outcome:
                with validator._client(config) as client:
                    assert client is fake_client
                    assert fake_client.started
                    assert client_factory.call_args is not None
                    options = client_factory.call_args.kwargs
                    assert options["use_ssl"] is True
                    assert options["hosts"] == _DATABAG["endpoints"]
                    assert isinstance(options["ca"], str)
                    ca_file = Path(options["ca"])
                    assert ca_file.is_absolute()
                    assert ca_file.name == "ca.pem"
                    assert ca_file.read_text(encoding="utf-8") == _TLS_SECRET["tls-ca"]
                    if fail_during_use:
                        raise RuntimeError("client operation failed")

        # THEN both the client and temporary CA directory are cleaned up.
        assert not fake_client.started
        assert fake_client.closed
        assert not ca_file.exists()
        assert not ca_file.parent.exists()

    def test_connection_config_refreshes_credential_secrets(self) -> None:
        validator, _ = _make_validator()
        user_secret = SecretStub(_USER_SECRET)
        tls_secret = SecretStub(_TLS_SECRET)
        with (
            patch.object(user_secret, "get_content", return_value=_USER_SECRET) as user_content,
            patch.object(tls_secret, "get_content", return_value=_TLS_SECRET) as tls_content,
            patch.object(
                validator.charm.model,
                "get_secret",
                side_effect=lambda *, id: {"secret://user": user_secret, "secret://tls": tls_secret}[id],
            ) as get_secret,
        ):
            config = validator._connection_config()

        assert config["username"] == _USER_SECRET["username"]
        assert config["password"] == _USER_SECRET["password"]
        get_secret.assert_any_call(id="secret://user")
        get_secret.assert_any_call(id="secret://tls")
        assert user_content.call_args.kwargs == {"refresh": True}
        assert tls_content.call_args.kwargs == {"refresh": True}

    def test_repeated_prepare_with_same_identifier_resets_canary(self) -> None:
        validator, fake_client = _make_validator()
        with patch("validators.zookeeper.validator.uuid.uuid4") as uuid4, _use_client(validator, fake_client):
            uuid4.return_value.int = 123
            uuid4.return_value.hex = "first-token"
            first_state = validator.prepare()
            path = f"/canary/{validator._canary_node_name(first_state.id)}"
            fake_client.set(path, b"later-value", version=1)
            uuid4.return_value.hex = "second-token"
            second_state = validator.prepare()
            result, advanced_state = validator.checkpoint(second_state)

        assert first_state.id == second_state.id == 123
        assert first_state.token != second_state.token
        assert result.status == "PASS"
        assert advanced_state.ref == 2


class TestCheckpoint:
    def test_matching_token_and_version_pass_and_advance(self) -> None:
        validator, fake_client = _make_validator()
        with _use_client(validator, fake_client):
            state = validator.prepare()
            result, next_state = validator.checkpoint(state)

        assert result.status == "PASS"
        assert next_state == PersistenceState(id=state.id, ref=2, token=state.token)
        assert fake_client.nodes[f"/canary/{validator._canary_node_name(state.id)}"] == (state.token.encode(), 2)

    @pytest.mark.parametrize("mismatch", ["version", "token", "missing"])
    def test_mismatch_fails_without_advancing(self, mismatch: str) -> None:
        validator, fake_client = _make_validator()
        with _use_client(validator, fake_client):
            state = validator.prepare()
            path = f"/canary/{validator._canary_node_name(state.id)}"
            if mismatch == "version":
                fake_client.nodes[path] = (state.token.encode(), 2)
            elif mismatch == "token":
                fake_client.nodes[path] = (b"recreated", 1)
            else:
                del fake_client.nodes[path]
            before = dict(fake_client.nodes)
            result, returned_state = validator.checkpoint(state)

        assert result.status == "FAIL"
        assert returned_state == state
        assert fake_client.nodes == before

    @pytest.mark.parametrize(
        "state",
        [
            PersistenceState(id=-1, ref=1, token="token"),
            PersistenceState(id=1, ref=0, token="token"),
            PersistenceState(id=1 << 63, ref=1, token="token"),
            PersistenceState(id=1, ref=1 << 31, token="token"),
        ],
    )
    def test_invalid_state_is_rejected(self, state: PersistenceState) -> None:
        validator, fake_client = _make_validator()
        with _use_client(validator, fake_client):
            with pytest.raises(ValueError):
                validator.checkpoint(state)
        assert not fake_client.nodes

    def test_empty_token_is_rejected_before_reading(self) -> None:
        validator, fake_client = _make_validator()
        malformed_state = PersistenceState.model_construct(id=1, ref=1, token="")
        with _use_client(validator, fake_client):
            with pytest.raises(ValueError, match="token"):
                validator.checkpoint(malformed_state)
        assert not fake_client.nodes


class TestCleanup:
    @pytest.mark.parametrize("missing_field", ["endpoints", "database", "username", "password", "tls-ca"])
    def test_incomplete_connection_fields_skip_without_connecting(self, missing_field: str) -> None:
        # GIVEN a relation with an incomplete connection configuration.
        validator, _ = _make_validator()
        if missing_field in {"endpoints", "database"}:
            del validator.relation.data[validator.relation.app][missing_field]
        else:
            secret = dict(_TLS_SECRET if missing_field == "tls-ca" else _USER_SECRET)
            del secret[missing_field]
            uri = "secret://tls" if missing_field == "tls-ca" else "secret://user"
            secrets = {"secret://user": SecretStub(_USER_SECRET), "secret://tls": SecretStub(_TLS_SECRET)}
            secrets[uri] = SecretStub(secret)
            with patch.object(validator.charm.model, "get_secret", side_effect=lambda *, id: secrets[id]):
                self._assert_cleanup_skips(validator)
            return

        # WHEN cleanup is requested, THEN it skips without opening a client.
        self._assert_cleanup_skips(validator)

    @staticmethod
    def _assert_cleanup_skips(validator: ZookeeperPersistenceValidator) -> None:
        with patch.object(validator, "_client") as client:
            with pytest.raises(PersistenceNotApplicable, match="incomplete"):
                validator.cleanup()
        client.assert_not_called()

    def test_blank_endpoints_skip_without_connecting(self) -> None:
        validator, _ = _make_validator()
        validator.relation.data[validator.relation.app]["endpoints"] = " , "
        self._assert_cleanup_skips(validator)

    @pytest.mark.parametrize("operation", ["start", "delete"])
    def test_operational_failures_propagate(self, operation: str) -> None:
        # GIVEN an existing canary and a client operation that fails.
        validator, fake_client = _make_validator()
        path = f"/canary/{validator._canary_node_name(1)}"
        fake_client.nodes[path] = (b"token", 1)
        with (
            patch("validators.zookeeper.validator.KazooClient", return_value=fake_client),
            patch.object(fake_client, operation, side_effect=NoAuthError),
        ):
            # WHEN cleanup runs, THEN the operational failure is not converted to a skip.
            with pytest.raises(NoAuthError):
                validator.cleanup()
        assert path in fake_client.nodes

    def test_deletes_exact_scoped_canaries_and_preserves_other_nodes(self) -> None:
        validator, fake_client = _make_validator()
        prefix = validator._canary_node_prefix()
        valid_name = f"{prefix}{12:020d}"
        fake_client.nodes.update(
            {
                f"/canary/{valid_name}": (b"token", 1),
                f"/canary/{prefix}backup": (b"unrelated", 1),
                f"/canary/{prefix}{1 << 63:020d}": (b"out-of-range", 1),
                "/canary/other-validator-node": (b"other", 1),
            }
        )
        with _use_client(validator, fake_client):
            validator.cleanup()

        assert f"/canary/{valid_name}" not in fake_client.nodes
        assert f"/canary/{prefix}backup" in fake_client.nodes
        assert f"/canary/{prefix}{1 << 63:020d}" in fake_client.nodes
        assert "/canary/other-validator-node" in fake_client.nodes

    def test_no_resources_is_a_noop(self) -> None:
        validator, fake_client = _make_validator()
        with _use_client(validator, fake_client):
            validator.cleanup()
        assert not fake_client.nodes

    def test_root_parent_path_uses_a_single_leading_slash(self) -> None:
        validator, fake_client = _make_validator()
        validator.relation.data[validator.relation.app]["database"] = "/"
        with _use_client(validator, fake_client):
            state = validator.prepare()

        assert f"/{validator._canary_node_name(state.id)}" in fake_client.nodes
