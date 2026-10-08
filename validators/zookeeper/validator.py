# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import re
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, cast

from kazoo.client import KazooClient  # type: ignore[import-untyped]
from kazoo.exceptions import BadVersionError, NoNodeError  # type: ignore[import-untyped]
from kazoo.security import make_acl  # type: ignore[import-untyped]

from validators.base import (
    BasePersistenceValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationCheck,
    ValidationResult,
)

_CANARY_NODE_PREFIX = "validator-persistence-canary-"
_MAX_CANARY_IDENTIFIER = (1 << 63) - 1
_MAX_ZNODE_VERSION = (1 << 31) - 1
_CLIENT_TIMEOUT_SECONDS = 10


class _IncompleteConnectionConfig(RuntimeError):
    pass


class _KazooStat(Protocol):
    version: int


class _KazooClient(Protocol):
    def start(self, timeout: int) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...

    def create(self, path: str, value: bytes, acl: list[object]) -> str: ...

    def get(self, path: str) -> tuple[bytes, _KazooStat]: ...

    def set(self, path: str, value: bytes, version: int) -> _KazooStat: ...

    def delete(self, path: str, recursive: bool = False) -> None: ...

    def get_children(self, path: str) -> list[str]: ...


class ZookeeperPersistenceValidator(BasePersistenceValidator):
    """Verify ZooKeeper zNode data survives disruptions for a zookeeper relation."""

    def prepare(self) -> PersistenceState:
        self._require_requires_role()
        config = self._connection_config()
        identifier = uuid.uuid4().int & _MAX_CANARY_IDENTIFIER
        token = uuid.uuid4().hex.encode()
        parent = self._parent_path(config["database"])
        canary_path = self._child_path(parent, self._canary_node_name(identifier))
        acl = [make_acl("sasl", config["username"], read=True, write=True, create=True)]

        with self._client(config) as client:
            try:
                client.delete(canary_path, recursive=True)
            except NoNodeError:
                pass
            client.create(canary_path, b"", acl=acl)
            # ZooKeeper starts a new node at version 0; seed version 1 so the
            # persisted ref has the same value as the version checked later.
            client.set(canary_path, token, version=0)

        return PersistenceState(id=identifier, ref=1, token=token.decode())

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        self._require_requires_role()
        canary_name = self._canary_node_name(expected.id)
        if not 1 <= expected.ref < _MAX_ZNODE_VERSION:
            raise ValueError(f"canary ref {expected.ref} is out of range " f"(expected 1..{_MAX_ZNODE_VERSION - 1})")
        if not expected.token:
            raise ValueError("canary token must not be empty")

        config = self._connection_config()
        canary_path = self._child_path(self._parent_path(config["database"]), canary_name)
        passed = False
        message = "Canary zNode is missing."
        with self._client(config) as client:
            try:
                data, stat = client.get(canary_path)
                passed = data == expected.token.encode() and stat.version == expected.ref
                message = (
                    f"Canary token and version {expected.ref} are present."
                    if passed
                    else f"Canary token/version mismatch: expected version {expected.ref}, found {stat.version}."
                )
                if passed:
                    # Use the expected version as a compare-and-set guard. A concurrent
                    # update must not advance state past the version the harness tracks.
                    client.set(canary_path, expected.token.encode(), version=expected.ref)
            except (NoNodeError, BadVersionError):
                passed = False
                message = f"Canary zNode did not match expected version {expected.ref}."

        result = self._make_result(
            level="deep",
            checks=[ValidationCheck(name="canary_znode", passed=passed, message=message)],
        )
        new_state = PersistenceState(id=expected.id, ref=expected.ref + 1, token=expected.token) if passed else expected
        return result, new_state

    def cleanup(self) -> None:
        self._require_requires_role()
        try:
            config = self._connection_config()
        except _IncompleteConnectionConfig as error:
            raise PersistenceNotApplicable(
                "Relation connection fields are incomplete; cleanup cannot remove canary data yet."
            ) from error
        parent = self._parent_path(config["database"])
        node_regex = re.compile(re.escape(self._canary_node_prefix()) + r"(?P<identifier>[0-9]{20})")

        with self._client(config) as client:
            try:
                children = client.get_children(parent)
            except NoNodeError:
                return
            for child in children:
                match = node_regex.fullmatch(child)
                if match is None or int(match.group("identifier")) > _MAX_CANARY_IDENTIFIER:
                    continue
                try:
                    client.delete(self._child_path(parent, child), recursive=True)
                except NoNodeError:
                    continue

    def _require_requires_role(self) -> None:
        if self.role != "requires":
            raise PersistenceNotApplicable(f"Role '{self.role}' is not supported by {self.__class__.__name__}.")

    def _connection_config(self) -> dict[str, str]:
        credentials = {
            **self._resolve_secret("secret-user", "username", "password", "uris"),
            **self._resolve_secret("secret-tls", "tls-ca"),
        }
        required = ["endpoints", "database", "username", "password"]
        if not self.validate_schema(required, credentials).passed:
            missing = [field for field in required if not (self.databag | credentials).get(field)]
            raise _IncompleteConnectionConfig(
                f"Cannot connect to ZooKeeper: missing relation fields {', '.join(missing)}"
            )

        config = self.databag | credentials
        endpoints = [endpoint.strip() for endpoint in config["endpoints"].split(",") if endpoint.strip()]
        if not endpoints:
            raise _IncompleteConnectionConfig("Cannot connect to ZooKeeper: 'endpoints' is blank")
        config["endpoints"] = ",".join(endpoints)
        if self.databag.get("secret-tls") and not config.get("tls-ca"):
            raise _IncompleteConnectionConfig("Cannot connect to ZooKeeper: TLS secret is missing 'tls-ca'")
        return config

    def _resolve_secret(self, uri_key: str, *fields: str) -> dict[str, str]:
        if uri := self.databag.get(uri_key):
            return self.charm.model.get_secret(id=uri).get_content(refresh=True)
        return self.resolve_secret(uri_key, *fields)

    @staticmethod
    def _parent_path(database: str) -> str:
        if not database.startswith("/") or "//" in database or any(part in {".", ".."} for part in database.split("/")):
            raise ValueError("ZooKeeper 'database' must be an absolute zNode path without '.' or '..' segments")
        return database.rstrip("/") or "/"

    @staticmethod
    def _child_path(parent: str, child: str) -> str:
        return f"{parent.rstrip('/')}/{child}"

    def _canary_node_prefix(self) -> str:
        scope = f"{self.charm.model.uuid}:{self.relation_id}:{self.charm.model.unit.name}"
        scope_token = hashlib.sha256(scope.encode()).hexdigest()[:16]
        return f"{_CANARY_NODE_PREFIX}{scope_token}_"

    def _canary_node_name(self, identifier: int) -> str:
        if not 0 <= identifier <= _MAX_CANARY_IDENTIFIER:
            raise ValueError(f"canary identifier {identifier} is out of range (expected 0..{_MAX_CANARY_IDENTIFIER})")
        return f"{self._canary_node_prefix()}{identifier:020d}"

    @contextmanager
    def _client(self, config: dict[str, str]) -> Iterator[_KazooClient]:
        tls_ca = config.get("tls-ca")
        with tempfile.TemporaryDirectory() as temp_dir:
            client_options: dict[str, object] = {
                "hosts": config["endpoints"],
                "timeout": _CLIENT_TIMEOUT_SECONDS,
                "use_ssl": bool(tls_ca),
                "sasl_options": {
                    "mechanism": "DIGEST-MD5",
                    "username": config["username"],
                    "password": config["password"],
                    "service": "zookeeper",
                    "principal": "zk-sasl-md5",
                },
            }
            if tls_ca:
                ca_file = Path(temp_dir) / "ca.pem"
                ca_file.write_text(tls_ca, encoding="utf-8")
                client_options["ca"] = str(ca_file)

            client = cast(_KazooClient, KazooClient(**client_options))
            try:
                client.start(timeout=_CLIENT_TIMEOUT_SECONDS)
                yield client
            finally:
                client.stop()
                client.close()
