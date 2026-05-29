# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import pymongo
from pymongo.errors import WriteError
from test_utils.stubs import (  # type: ignore[import-not-found]
    ApplicationStub,
    RelationStub,
)
from test_utils.helpers import make_charm_from_relation

from validators.mongodb_client.validator import MongoDBClientValidator

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _make_validator(databag: dict[str, str], endpoint: str = "db") -> MongoDBClientValidator:
    app = ApplicationStub()
    relation = RelationStub(app=app, data={app: databag}, name=endpoint, id=0)
    charm = make_charm_from_relation(relation, interface_name="mongodb_client")
    return MongoDBClientValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


@dataclass
class AdminStub:
    """Minimal stand-in for the admin database; command() raises command_error if set."""

    command_error: Exception | None = None

    def command(self, cmd: str) -> None:
        if self.command_error:
            raise self.command_error


@dataclass
class InsertResultStub:
    """Minimal stand-in for insert result."""

    inserted_id: str = "test_id_1"


@dataclass
class CollectionStub:
    """Minimal stand-in for a MongoDB collection."""

    insert_error: Exception | None = None
    find_error: Exception | None = None
    drop_error: Exception | None = None

    def insert_one(self, document: dict[str, Any]) -> InsertResultStub:
        if self.insert_error:
            raise self.insert_error
        return InsertResultStub()

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        if self.find_error:
            raise self.find_error
        return {"_id": query.get("_id"), "_test": True}


@dataclass
class DatabaseStub:
    """Minimal stand-in for a MongoDB database."""

    list_collections_error: Exception | None = None
    collection_stub: CollectionStub = field(default_factory=CollectionStub)

    def list_collections(self) -> list[str]:
        if self.list_collections_error:
            raise self.list_collections_error
        return []

    def __getitem__(self, name: str) -> CollectionStub:
        return self.collection_stub

    def drop_collection(self, name: str) -> None:
        pass


@dataclass
class MongoClientStub:
    """Minimal connection stub; database_stub is returned by __getitem__()."""

    admin_stub: AdminStub = field(default_factory=AdminStub)
    database_stub: DatabaseStub = field(default_factory=DatabaseStub)

    @property
    def admin(self) -> AdminStub:
        return self.admin_stub

    def __getitem__(self, name: str) -> DatabaseStub:
        return self.database_stub

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "endpoints": "10.1.2.3:27017",
    "database": "mydb",
    "username": "myuser",
    "password": "mypassword",
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMongoDBClientValidatorSimple:
    def test_returns_skipped_for_unsupported_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None
        assert "not supported" in result.error

    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        # GIVEN a databag with missing required fields
        validator = _make_validator({"endpoints": "10.1.2.3:5432"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "database" in schema_check.message
        assert "username" in schema_check.message
        assert "password" in schema_check.message

    def test_passes_schema_check_with_all_required_fields(self) -> None:
        # GIVEN a complete databag and a successful DB connection
        validator = _make_validator(VALID_DATABAG)
        client = MongoClientStub(admin_stub=AdminStub())
        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed

    def test_fails_connect_check_when_db_unreachable(self) -> None:
        # GIVEN a complete databag but a DB that refuses connections
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.mongodb_client.validator.MongoClient",
            side_effect=pymongo.errors.ConnectionFailure("Connection refused"),
        ):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "Connection refused" in connect_check.message

    def test_fails_query_check_when_collection_raises(self) -> None:
        # GIVEN a connection that succeeds but the canary query raises
        validator = _make_validator(VALID_DATABAG)
        conn = MongoClientStub(
            database_stub=DatabaseStub(list_collections_error=pymongo.errors.PyMongoError("query error"))
        )

        with patch("validators.mongodb_client.validator.MongoClient", return_value=conn):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        query_check = next(c for c in result.checks if c.name == "query")
        assert not query_check.passed

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-endpoint")

        with patch("validators.mongodb_client.validator.MongoClient", return_value=MongoClientStub()):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-endpoint"
        assert result.interface == "mongodb_client"


class TestMongoDBClientValidatorDeep:
    def test_returns_skipped_for_uat_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_deep_passes_on_successful_write_read_verify(self) -> None:
        # GIVEN a complete databag and a successful connection with write/read
        validator = _make_validator(VALID_DATABAG)
        conn = MongoClientStub(database_stub=DatabaseStub(collection_stub=CollectionStub()))
        with patch("validators.mongodb_client.validator.MongoClient", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        assert result.level == "deep"
        write_check = next(c for c in result.checks if c.name == "write_read_verify")
        assert write_check.passed
        cleanup_check = next(c for c in result.checks if c.name == "cleanup")
        assert cleanup_check.passed
        latency_check = next(c for c in result.checks if c.name == "latency")
        assert latency_check.passed

    def test_deep_fails_when_write_fails(self) -> None:
        # GIVEN a connection that fails on insert_one
        validator = _make_validator(VALID_DATABAG)
        conn = MongoClientStub(
            database_stub=DatabaseStub(collection_stub=CollectionStub(insert_error=WriteError("Write failed")))
        )
        with patch("validators.mongodb_client.validator.MongoClient", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        write_check = next(c for c in result.checks if c.name == "write_read_verify")
        assert not write_check.passed

    def test_deep_fails_when_read_verification_fails(self) -> None:
        # GIVEN a connection where find_one returns a doc without the _test field
        @dataclass
        class FailingCollectionStub(CollectionStub):
            def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
                return {"_id": query.get("_id")}  # Missing _test field

        validator = _make_validator(VALID_DATABAG)
        conn = MongoClientStub(database_stub=DatabaseStub(collection_stub=FailingCollectionStub()))
        with patch("validators.mongodb_client.validator.MongoClient", return_value=conn):
            # WHEN
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        write_check = next(c for c in result.checks if c.name == "write_read_verify")
        assert not write_check.passed
        assert "Failed to verify" in write_check.message
