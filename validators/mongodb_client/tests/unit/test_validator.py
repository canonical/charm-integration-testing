# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import re
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import pymongo
import pytest
from pydantic import ValidationError
from pymongo.errors import WriteError

from validators.base import PersistenceNotApplicable, PersistenceState
from validators.mongodb_client.validator import (
    MongoDBClientPersistenceValidator,
    MongoDBClientValidator,
)
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

# Arbitrary non-empty token used by checkpoint() tests; prepare() generates a random one per run.
TEST_TOKEN = "test-token-abc123"


def _make_validator(
    databag: dict[str, str], endpoint: str = "db", role: RelationRoleStub = RelationRoleStub.requires
) -> MongoDBClientValidator:
    app = ApplicationStub()
    relation = RelationStub(app=app, data={app: databag}, name=endpoint, id=0)
    charm = make_charm_from_relation(relation, interface_name="mongodb_client", role=role)
    return MongoDBClientValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def _make_persistence_validator(
    databag: dict[str, str],
    endpoint: str = "db",
    role: RelationRoleStub = RelationRoleStub.requires,
    relation_id: int = 0,
    model_uuid: str = "11111111-1111-1111-1111-111111111111",
    unit_name: str = "app/0",
) -> MongoDBClientPersistenceValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=relation_id, app=app, data={app: databag})
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(
            relation,
            interface_name="mongodb_client",
            role=role,
            local_model_uuid=model_uuid,
            local_unit_name=unit_name,
        ),
    )
    return MongoDBClientPersistenceValidator(charm, cast(ops.Relation, relation))


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


@dataclass
class PersistenceCollectionStub:
    """Minimal stand-in for a canary collection: tracks inserted documents in-memory."""

    documents: list[dict[str, Any]] = field(default_factory=list)
    insert_error: Exception | None = None
    count_error: Exception | None = None

    def insert_one(self, document: dict[str, Any]) -> InsertResultStub:
        if self.insert_error:
            raise self.insert_error
        self.documents.append(document)
        return InsertResultStub()

    def count_documents(self, query: dict[str, Any]) -> int:
        if self.count_error:
            raise self.count_error
        marker = query.get("marker")
        return sum(1 for d in self.documents if d.get("marker") == marker)

    def find(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        if self.count_error:
            raise self.count_error
        marker = query.get("marker")
        return [d for d in self.documents if d.get("marker") == marker]


@dataclass
class PersistenceDatabaseStub:
    """Minimal stand-in for a MongoDB database backing the persistence validator.

    Collections are created lazily on first ``__getitem__`` access (matching real MongoDB, where
    writing to a not-yet-existing collection creates it), and ``drop_collection`` removes them
    entirely so a later ``__getitem__`` starts fresh, mirroring the real backend.
    """

    collections: dict[str, PersistenceCollectionStub] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    drop_error: Exception | None = None

    def __getitem__(self, name: str) -> PersistenceCollectionStub:
        return self.collections.setdefault(name, PersistenceCollectionStub())

    def drop_collection(self, name: str) -> None:
        if self.drop_error:
            raise self.drop_error
        self.dropped.append(name)
        self.collections.pop(name, None)

    def list_collection_names(self, filter: dict[str, Any] | None = None) -> list[str]:  # noqa: A002
        if filter is None:
            return list(self.collections)
        pattern = filter["name"]["$regex"]
        return [name for name in self.collections if re.match(pattern, name)]


@dataclass
class PersistenceMongoClientStub:
    """Minimal connection stub for persistence tests; database_stub is returned by __getitem__()."""

    database_stub: PersistenceDatabaseStub = field(default_factory=PersistenceDatabaseStub)
    getitem_error: Exception | None = None
    close_called: bool = False

    def __getitem__(self, name: str) -> PersistenceDatabaseStub:
        if self.getitem_error:
            raise self.getitem_error
        return self.database_stub

    def close(self) -> None:
        self.close_called = True


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
    @pytest.mark.parametrize(
        "role,should_skip",
        [(RelationRoleStub.requires, False), (RelationRoleStub.provides, True), (RelationRoleStub.peer, True)],
    )
    def test_returns_skipped_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, role=role)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

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


class TestMongoDBClientPersistenceValidatorRole:
    @pytest.mark.parametrize("role", [RelationRoleStub.provides, RelationRoleStub.peer])
    def test_prepare_raises_not_applicable_for_non_requires_role(self, role: RelationRoleStub) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG, role=role)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.prepare()

    def test_checkpoint_raises_not_applicable_for_non_requires_role(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG, role=RelationRoleStub.provides)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

    def test_cleanup_raises_not_applicable_for_non_requires_role(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG, role=RelationRoleStub.provides)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.cleanup()


class TestMongoDBClientPersistenceValidatorPrepare:
    def test_drops_and_recreates_canary_collection_and_returns_state(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            state = validator.prepare()

        # THEN
        assert isinstance(state, PersistenceState)
        assert state.ref == 1
        assert state.token
        collection_name = f"validator_canary_da7d88bc9ad4d4fd_{state.id:020d}"
        # prepare() drops any leftover collection before recreating it
        assert collection_name in client.database_stub.dropped
        documents = client.database_stub.collections[collection_name].documents
        assert len(documents) == 1
        assert documents[0]["marker"] == state.token
        assert documents[0]["checkpoint_ref"] == 1

    def test_generates_distinct_identifiers_across_calls(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            first = validator.prepare()
            second = validator.prepare()

        # THEN
        assert first.id != second.id
        assert first.token != second.token

    def test_idempotent_when_identifier_is_forced_to_repeat(self) -> None:
        # GIVEN prepare() is called twice with the same (forced) identifier - e.g. a re-run of a
        # failed prepare(), or leftover state from a previous process
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()
        fixed_identifier = 12345

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            with patch("validators.mongodb_client.validator.uuid.uuid4") as mock_uuid4:
                mock_uuid4.return_value.int = fixed_identifier
                mock_uuid4.return_value.hex = "fixed-token-1"
                first = validator.prepare()
                mock_uuid4.return_value.hex = "fixed-token-2"
                second = validator.prepare()

        # THEN the second prepare() still produces a usable, freshly-seeded canary collection
        assert first.id == second.id == fixed_identifier
        assert second.token == "fixed-token-2"
        collection_name = f"validator_canary_da7d88bc9ad4d4fd_{fixed_identifier:020d}"
        documents = client.database_stub.collections[collection_name].documents
        assert len(documents) == 1
        assert documents[0]["marker"] == second.token

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN checkpoint() is run against the second state
            result, _ = validator.checkpoint(second)

        # THEN it passes, proving the collection left behind by prepare() is usable
        assert result.status == "PASS"

    def test_raises_when_endpoints_is_blank(self) -> None:
        # GIVEN
        databag = {**VALID_DATABAG, "endpoints": ""}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="Missing"):
            validator.prepare()

    def test_raises_when_required_field_is_missing(self) -> None:
        # GIVEN
        databag = {"endpoints": "10.1.2.3:27017"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError):
            validator.prepare()


class TestMongoDBClientPersistenceValidatorCheckpoint:
    def test_passes_when_document_count_matches_expected_ref(self) -> None:
        # GIVEN the canary collection has exactly the expected number of matching documents
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()
        collection_name = "validator_canary_da7d88bc9ad4d4fd_00000000000000000042"
        collection = client.database_stub[collection_name]
        collection.documents = [
            {"marker": TEST_TOKEN, "checkpoint_ref": 1},
            {"marker": TEST_TOKEN, "checkpoint_ref": 2},
        ]

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN
        assert result.status == "PASS"
        check = next(c for c in result.checks if c.name == "document_count")
        assert check.passed
        assert new_state.id == 42
        assert new_state.ref == 3
        assert new_state.token == TEST_TOKEN
        # A new document is still written to continue the chain
        assert len(collection.documents) == 3
        assert collection.documents[-1]["marker"] == TEST_TOKEN
        assert collection.documents[-1]["checkpoint_ref"] == 3

    def test_fails_when_document_count_is_lower_than_expected(self) -> None:
        # GIVEN data loss: fewer matching documents than expected
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()
        collection_name = "validator_canary_da7d88bc9ad4d4fd_00000000000000000007"
        collection = client.database_stub[collection_name]
        collection.documents = [{"marker": TEST_TOKEN, "checkpoint_ref": 1}]

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=7, ref=3))

        # THEN a FAIL result (not an exception) is returned
        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "document_count")
        assert not check.passed
        assert "3" in check.message and "1" in check.message
        # On FAIL the returned state must match `expected` unchanged and no document is written,
        # so a later retry re-checks the same expected count instead of drifting.
        assert new_state == PersistenceState(token=TEST_TOKEN, id=7, ref=3)
        assert len(collection.documents) == 1

    def test_fails_when_collection_is_not_found(self) -> None:
        # GIVEN the canary collection doesn't exist (e.g. it was dropped/never created)
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=7, ref=1))

        # THEN
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=7, ref=1)

    def test_filters_document_count_by_token(self) -> None:
        # GIVEN a collection recreated from scratch with an unrelated but equally-sized set of
        # documents - only the random token distinguishes the original canary from the recreated one
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()
        collection_name = "validator_canary_da7d88bc9ad4d4fd_00000000000000000099"
        collection = client.database_stub[collection_name]
        collection.documents = [{"marker": "a-different-token", "checkpoint_ref": 1}]

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN the recreated collection is detected as data loss, not a false PASS
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=99, ref=1)

    def test_fails_when_a_same_token_ref_is_duplicated_alongside_a_missing_one(self) -> None:
        # GIVEN a collection with the right document *count* for expected.ref, but where that
        # count is reached via a duplicated ref (1) rather than the real, distinct refs (1..2)
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()
        collection_name = "validator_canary_da7d88bc9ad4d4fd_00000000000000000042"
        collection = client.database_stub[collection_name]
        collection.documents = [
            {"marker": TEST_TOKEN, "checkpoint_ref": 1},
            {"marker": TEST_TOKEN, "checkpoint_ref": 1},
        ]

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN a bare count-only check would have falsely passed here (2 matching documents == 2
        # expected); the exact-ref-set check must still fail since ref 2 is missing.
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=2)

    def test_fails_when_a_same_token_ref_has_a_malformed_value(self) -> None:
        # GIVEN a same-token document whose checkpoint_ref isn't a real int (e.g. corrupted data)
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()
        collection_name = "validator_canary_da7d88bc9ad4d4fd_00000000000000000042"
        collection = client.database_stub[collection_name]
        collection.documents = [{"marker": TEST_TOKEN, "checkpoint_ref": "1"}]

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=1))

        # THEN the malformed ref is not silently ignored or allowed to coincidentally pass
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=1)

    def test_fails_fast_without_allocating_an_unbounded_range_for_a_huge_expected_ref(self) -> None:
        # GIVEN a malformed/restored state with an enormous expected.ref (e.g. a corrupted --refs
        # payload) and a collection with only a few real matching documents
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()
        collection_name = "validator_canary_da7d88bc9ad4d4fd_00000000000000000042"
        collection = client.database_stub[collection_name]
        collection.documents = [{"marker": TEST_TOKEN, "checkpoint_ref": 1}]
        huge_ref = 10**9

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN/THEN this must return promptly rather than materializing a list of
            # `huge_ref` elements to compare against
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=huge_ref))

        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=huge_ref)

    def test_rejects_state_without_a_token(self) -> None:
        # GIVEN a state serialised before the token existed (or otherwise restored/malformed).
        # The base protocol rejects such a state at construction, so it can never reach checkpoint().
        with pytest.raises(ValidationError):
            PersistenceState(id=1, ref=1)

    def test_result_endpoint_and_interface_are_set(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG, endpoint="my-db")
        client = PersistenceMongoClientStub()
        collection_name = "validator_canary_da7d88bc9ad4d4fd_00000000000000000001"
        client.database_stub[collection_name].documents = [{"marker": TEST_TOKEN, "checkpoint_ref": 1}]

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            result, _ = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

        # THEN
        assert result.endpoint == "my-db"
        assert result.interface == "mongodb_client"
        assert result.level == "deep"

    def test_raises_when_expected_identifier_is_out_of_range(self) -> None:
        # GIVEN a restored/malformed PersistenceState with an out-of-range id
        validator = _make_persistence_validator(VALID_DATABAG)
        out_of_range_id = 1 << 63  # one past _MAX_CANARY_IDENTIFIER
        client = PersistenceMongoClientStub()

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=out_of_range_id, ref=1))

    def test_raises_when_expected_identifier_is_negative(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=-1, ref=1))

    def test_raises_when_expected_ref_is_zero(self) -> None:
        # GIVEN a restored/malformed PersistenceState with ref=0 (prepare() always returns ref=1)
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=0))

    def test_raises_when_expected_ref_is_negative(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=-1))


class TestMongoDBClientPersistenceValidatorOpenDatabase:
    def test_cleans_up_client_and_ca_file_when_database_selection_raises(self) -> None:
        # GIVEN an invalid database name that pymongo rejects only once selected (client[...]),
        # not up front by validate_schema()/MongoClient() construction
        databag = {**VALID_DATABAG, "tls-ca": "fake-ca-content"}
        validator = _make_persistence_validator(databag)
        client = PersistenceMongoClientStub(getitem_error=ValueError("invalid database name"))

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            with pytest.raises(ValueError, match="invalid database name"):
                validator._open_database()

        # THEN the client and its temporary CA file are cleaned up rather than leaked, even though
        # the failure happens before the caller's own try/finally scope is entered
        assert client.close_called
        assert validator.ca_file_path is None


class TestMongoDBClientPersistenceValidatorCleanup:
    def test_drops_all_discovered_canary_collections(self) -> None:
        # GIVEN two leftover canary collections belonging to this relation/unit
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()
        name_1 = "validator_canary_da7d88bc9ad4d4fd_00000000000000000001"
        name_2 = "validator_canary_da7d88bc9ad4d4fd_00000000000000000002"
        client.database_stub[name_1]
        client.database_stub[name_2]

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            validator.cleanup()

        # THEN
        assert set(client.database_stub.dropped) == {name_1, name_2}
        assert name_1 not in client.database_stub.collections
        assert name_2 not in client.database_stub.collections

    def test_noop_when_no_canary_collections_exist(self) -> None:
        # GIVEN no canary collections exist
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN / THEN (must not raise)
            validator.cleanup()

        assert client.database_stub.dropped == []

    def test_scopes_discovery_to_this_relations_id(self) -> None:
        # GIVEN a canary collection belonging to a different relation id
        other_relation_validator = _make_persistence_validator(VALID_DATABAG, relation_id=99)
        client = PersistenceMongoClientStub()
        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            other_relation_validator.prepare()
        other_collection = next(iter(client.database_stub.collections))

        validator = _make_persistence_validator(VALID_DATABAG, relation_id=0)
        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            validator.cleanup()

        # THEN the other relation's canary collection is untouched. (It was already recorded in
        # `dropped` by its own prepare()'s idempotent DROP-then-CREATE, so check existence instead.)
        assert other_collection in client.database_stub.collections

    def test_scopes_discovery_to_this_models_uuid(self) -> None:
        # GIVEN a canary collection belonging to a different model
        other_model_validator = _make_persistence_validator(
            VALID_DATABAG, model_uuid="22222222-2222-2222-2222-222222222222"
        )
        client = PersistenceMongoClientStub()
        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            other_model_validator.prepare()
        other_collection = next(iter(client.database_stub.collections))

        validator = _make_persistence_validator(VALID_DATABAG)
        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            validator.cleanup()

        # THEN. (It was already recorded in `dropped` by its own prepare()'s idempotent
        # DROP-then-CREATE, so check existence instead.)
        assert other_collection in client.database_stub.collections

    def test_scopes_discovery_to_this_unit(self) -> None:
        # GIVEN a canary collection belonging to a different unit of the same application
        other_unit_validator = _make_persistence_validator(VALID_DATABAG, unit_name="app/1")
        client = PersistenceMongoClientStub()
        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            other_unit_validator.prepare()
        other_collection = next(iter(client.database_stub.collections))

        validator = _make_persistence_validator(VALID_DATABAG, unit_name="app/0")
        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            validator.cleanup()

        # THEN. (It was already recorded in `dropped` by its own prepare()'s idempotent
        # DROP-then-CREATE, so check existence instead.)
        assert other_collection in client.database_stub.collections

    def test_rejects_discovered_collections_that_only_share_the_prefix(self) -> None:
        # GIVEN a hand-created collection that shares the discovery prefix but not the exact shape
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()
        lookalike = "validator_canary_da7d88bc9ad4d4fd_backup"
        client.database_stub[lookalike]

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            validator.cleanup()

        # THEN the unrelated collection survives
        assert lookalike in client.database_stub.collections
        assert lookalike not in client.database_stub.dropped

    def test_rejects_discovered_collections_with_an_out_of_range_identifier(self) -> None:
        # GIVEN a shape-only look-alike with an identifier larger than prepare() could produce
        validator = _make_persistence_validator(VALID_DATABAG)
        client = PersistenceMongoClientStub()
        lookalike = "validator_canary_da7d88bc9ad4d4fd_99999999999999999999"
        client.database_stub[lookalike]

        with patch("validators.mongodb_client.validator.MongoClient", return_value=client):
            # WHEN
            validator.cleanup()

        # THEN it is not dropped
        assert lookalike in client.database_stub.collections
        assert lookalike not in client.database_stub.dropped

    def test_noop_when_no_credentials_present(self) -> None:
        # GIVEN a databag without any credential fields (e.g. relation already gone)
        validator = _make_persistence_validator({})

        with patch("validators.mongodb_client.validator.MongoClient") as mock_mongo_client:
            # WHEN / THEN
            with pytest.raises(PersistenceNotApplicable):
                validator.cleanup()

        # THEN no connection was attempted
        mock_mongo_client.assert_not_called()

    def test_noop_when_endpoints_present_but_other_required_fields_are_missing(self) -> None:
        # GIVEN a relation with "endpoints" but not yet database/username/password (still
        # mid-setup). Regression test for: a no-op guard checking only "endpoints" would fall
        # through to _open_database() and raise instead of no-op'ing.
        validator = _make_persistence_validator({"endpoints": "10.1.2.3:27017"})

        with patch("validators.mongodb_client.validator.MongoClient") as mock_mongo_client:
            # WHEN / THEN
            with pytest.raises(PersistenceNotApplicable):
                validator.cleanup()

        # THEN no connection was attempted
        mock_mongo_client.assert_not_called()
