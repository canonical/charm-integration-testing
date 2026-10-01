# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import re
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import pytest
from opensearchpy.exceptions import NotFoundError

from validators.base import PersistenceNotApplicable, PersistenceState
from validators.opensearch_client.validator import (
    _KIND_VALUE,
    OpenSearchClientPersistenceValidator,
    OpenSearchClientValidator,
    _exact_match_filter,
)
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "endpoints": "10.0.0.1:9200",
    "username": "test-user",
    "password": "test-pass",
    "index": "test-index",
}


def _make_validator(
    databag: dict[str, str],
    endpoint: str = "opensearch",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> OpenSearchClientValidator:
    app = ApplicationStub()
    relation = RelationStub(app=app, data={app: databag}, name=endpoint, id=0)
    charm = make_charm_from_relation(relation, interface_name="opensearch_client", role=role)
    return OpenSearchClientValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def _make_validator_no_app(endpoint: str = "opensearch") -> OpenSearchClientValidator:
    """Factory that produces a validator with no remote application on the relation."""
    relation = RelationStub(app=None, data={}, name=endpoint, id=0)
    charm = make_charm_from_relation(relation, interface_name="opensearch_client", role=RelationRoleStub.requires)
    return OpenSearchClientValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


# Arbitrary non-empty token used by checkpoint() tests; prepare() generates a random one per run.
TEST_TOKEN = "test-token-abc123"


def _make_persistence_validator(
    databag: dict[str, str],
    endpoint: str = "opensearch",
    role: RelationRoleStub = RelationRoleStub.requires,
    relation_id: int = 0,
    model_uuid: str = "11111111-1111-1111-1111-111111111111",
    unit_name: str = "app/0",
) -> OpenSearchClientPersistenceValidator:
    app = ApplicationStub()
    relation = RelationStub(app=app, data={app: databag}, name=endpoint, id=relation_id)
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(
            relation,
            interface_name="opensearch_client",
            role=role,
            local_model_uuid=model_uuid,
            local_unit_name=unit_name,
        ),
    )
    return OpenSearchClientPersistenceValidator(charm, cast(ops.Relation, relation))


# ---------------------------------------------------------------------------
# OpenSearch client stub
# ---------------------------------------------------------------------------


@dataclass
class ClusterStub:
    """Minimal stand-in for the OpenSearch cluster namespace."""

    health_response: dict[str, Any] = field(default_factory=lambda: {"status": "green"})
    health_error: Exception | None = None

    def health(self, **kwargs: Any) -> dict[str, Any]:
        if self.health_error:
            raise self.health_error
        return self.health_response


@dataclass
class IndicesStub:
    """Minimal stand-in for the OpenSearch indices namespace."""

    create_error: Exception | None = None
    delete_error: Exception | None = None

    def create(self, **kwargs: Any) -> None:
        if self.create_error:
            raise self.create_error

    def delete(self, **kwargs: Any) -> None:
        if self.delete_error:
            raise self.delete_error


@dataclass
class OpenSearchClientStub:
    """Minimal stand-in for an opensearch-py OpenSearch client."""

    cluster: ClusterStub = field(default_factory=ClusterStub)
    indices: IndicesStub = field(default_factory=IndicesStub)
    index_error: Exception | None = None
    get_response: dict[str, Any] = field(default_factory=lambda: {"_source": {"canary": True}})
    get_error: Exception | None = None
    delete_error: Exception | None = None

    def index(self, **kwargs: Any) -> None:
        if self.index_error:
            raise self.index_error

    def get(self, **kwargs: Any) -> dict[str, Any]:
        if self.get_error:
            raise self.get_error
        return self.get_response

    def delete(self, **kwargs: Any) -> None:
        if self.delete_error:
            raise self.delete_error

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# OpenSearch persistence-validator client stub
# ---------------------------------------------------------------------------


@dataclass
class PersistenceIndexStub:
    """In-memory stand-in for the single, charm-granted index: tracks documents by id."""

    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    _next_id: int = 0

    def add(self, body: dict[str, Any]) -> str:
        doc_id = f"auto-{self._next_id}"
        self._next_id += 1
        self.documents[doc_id] = dict(body)
        return doc_id


def _query_matches(clause: dict[str, Any], doc: dict[str, Any]) -> bool:
    """Recursively evaluate a query clause against a document.

    Understands the shapes the validator's count()/search() calls use: a plain
    ``{"term": {field: value}}``, a conjunction ``{"bool": {"filter": [...]}}``, and a disjunction
    ``{"bool": {"should": [...], "minimum_should_match": 1}}`` (used by ``_exact_match_filter()`` to
    match a field regardless of whether OpenSearch mapped it as ``keyword`` or analyzed ``text``
    with a ``.keyword`` multi-field). Any ``.keyword`` suffix is stripped to match against the
    plain field name documents are stored under, mirroring how OpenSearch's dynamic ".keyword"
    multi-field reflects the same value as its parent field.
    """
    if "term" in clause:
        field_name, value = next(iter(clause["term"].items()))
        return bool(doc.get(field_name.removesuffix(".keyword")) == value)
    bool_clause = clause["bool"]
    if "filter" in bool_clause:
        return all(_query_matches(c, doc) for c in bool_clause["filter"])
    return any(_query_matches(c, doc) for c in bool_clause["should"])


def _matches_query(body: dict[str, Any] | None, doc: dict[str, Any]) -> bool:
    if not body:
        return True
    return _query_matches(body["query"], doc)


@dataclass
class PersistenceOpenSearchClientStub:
    """Minimal stand-in for an opensearch-py OpenSearch client, for persistence tests.

    Models a single, shared index (the one the charm grants over the relation) rather than one
    the validator creates and drops itself - see ``OpenSearchClientPersistenceValidator``.
    """

    indices: dict[str, PersistenceIndexStub] = field(default_factory=dict)
    index_error: Exception | None = None
    count_error: Exception | None = None
    search_error: Exception | None = None
    delete_error: Exception | None = None

    def index(self, index: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if self.index_error:
            raise self.index_error
        # OpenSearch auto-creates an index on first document write if it doesn't already exist -
        # mirror that so a test can index into a not-yet-created index.
        doc_id = self.indices.setdefault(index, PersistenceIndexStub()).add(body)
        return {"_id": doc_id, "result": "created"}

    def count(self, index: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        if self.count_error:
            raise self.count_error
        if index not in self.indices:
            raise NotFoundError(404, "index_not_found_exception")
        matching = sum(1 for doc in self.indices[index].documents.values() if _matches_query(body, doc))
        return {"count": matching}

    def search(self, index: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        if self.search_error:
            raise self.search_error
        if index not in self.indices:
            raise NotFoundError(404, "index_not_found_exception")
        # Sort key mirrors OpenSearch's "_doc" sort: the document's position in internal (here,
        # insertion) order, not a value derived from "_id" - the validator must not rely on "_id"
        # fielddata, which is disabled by default on a real cluster.
        matches = [
            (position, doc_id)
            for position, (doc_id, doc) in enumerate(self.indices[index].documents.items())
            if _matches_query(body, doc)
        ]
        search_after = (body or {}).get("search_after")
        if search_after is not None:
            matches = [(position, doc_id) for position, doc_id in matches if position > search_after[0]]
        size = (body or {}).get("size")
        if size is not None:
            matches = matches[:size]
        hits = [
            {"_id": doc_id, "_source": self.indices[index].documents[doc_id], "sort": [position]}
            for position, doc_id in matches
        ]
        return {"hits": {"hits": hits}}

    def delete(self, index: str, id: str, **kwargs: Any) -> None:
        if self.delete_error:
            raise self.delete_error
        store = self.indices.get(index)
        if store is None or id not in store.documents:
            raise NotFoundError(404, "document_missing_exception")
        del store.documents[id]

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Simple (L1) tests
# ---------------------------------------------------------------------------


class TestOpenSearchClientValidatorSimple:
    def test_happy_path_pass(self) -> None:
        # GIVEN a complete databag and a healthy cluster
        validator = _make_validator(VALID_DATABAG)
        stub_client = OpenSearchClientStub()

        with (
            patch.object(OpenSearchClientValidator, "_build_client", return_value=stub_client),
            patch.object(OpenSearchClientValidator, "_remove_ca_file"),
        ):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        assert result.level == "simple"
        health_check = next(c for c in result.checks if c.name == "cluster_health")
        assert health_check.passed
        assert "green" in health_check.message

    def test_yellow_health_passes(self) -> None:
        # GIVEN a cluster reporting yellow health
        validator = _make_validator(VALID_DATABAG)
        stub_client = OpenSearchClientStub(cluster=ClusterStub(health_response={"status": "yellow"}))

        with (
            patch.object(OpenSearchClientValidator, "_build_client", return_value=stub_client),
            patch.object(OpenSearchClientValidator, "_remove_ca_file"),
        ):
            result = validator.validate(level="simple")

        assert result.status == "PASS"
        health_check = next(c for c in result.checks if c.name == "cluster_health")
        assert health_check.passed
        assert "yellow" in health_check.message

    def test_red_health_fails(self) -> None:
        # GIVEN a cluster reporting red health
        validator = _make_validator(VALID_DATABAG)
        stub_client = OpenSearchClientStub(cluster=ClusterStub(health_response={"status": "red"}))

        with (
            patch.object(OpenSearchClientValidator, "_build_client", return_value=stub_client),
            patch.object(OpenSearchClientValidator, "_remove_ca_file"),
        ):
            result = validator.validate(level="simple")

        assert result.status == "FAIL"
        health_check = next(c for c in result.checks if c.name == "cluster_health")
        assert not health_check.passed
        assert "red" in health_check.message

    def test_connection_error_fails(self) -> None:
        # GIVEN the cluster is unreachable
        validator = _make_validator(VALID_DATABAG)
        stub_client = OpenSearchClientStub(cluster=ClusterStub(health_error=Exception("connection refused")))

        with (
            patch.object(OpenSearchClientValidator, "_build_client", return_value=stub_client),
            patch.object(OpenSearchClientValidator, "_remove_ca_file"),
        ):
            result = validator.validate(level="simple")

        assert result.status == "FAIL"
        health_check = next(c for c in result.checks if c.name == "cluster_health")
        assert not health_check.passed
        assert "connection refused" in health_check.message

    def test_fails_when_endpoints_missing(self) -> None:
        # GIVEN databag is missing endpoints
        validator = _make_validator({"username": "u", "password": "p"})

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "endpoints" in schema_check.message

    def test_fails_when_credentials_missing(self) -> None:
        # GIVEN databag is missing username and password
        validator = _make_validator({"endpoints": "10.0.0.1:9200"})

        result = validator.validate(level="simple")

        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed

    def test_errors_when_no_remote_app(self) -> None:
        # GIVEN no remote application on the relation
        validator = _make_validator_no_app()

        result = validator.validate(level="simple")

        assert result.status == "ERROR"
        assert result.error is not None

    def test_skipped_for_uat_level(self) -> None:
        # GIVEN a valid databag
        validator = _make_validator(VALID_DATABAG)

        result = validator.validate(level="uat")

        assert result.status == "SKIPPED"
        assert result.error is not None

    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.requires, False),
            (RelationRoleStub.provides, True),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_skips_non_requires_roles(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN a validator with the specified role
        validator = _make_validator(VALID_DATABAG, role=role)
        stub_client = OpenSearchClientStub()

        with (
            patch.object(OpenSearchClientValidator, "_build_client", return_value=stub_client),
            patch.object(OpenSearchClientValidator, "_remove_ca_file"),
        ):
            result = validator.validate(level="simple")

        assert (result.status == "SKIPPED") == should_skip

    def test_fails_when_build_client_raises(self) -> None:
        # GIVEN _build_client() raises (e.g. bad TLS config)
        validator = _make_validator(VALID_DATABAG)

        with patch.object(OpenSearchClientValidator, "_build_client", side_effect=Exception("TLS init failed")):
            result = validator.validate(level="simple")

        # THEN the result is FAIL with a connect check — not an unhandled ERROR
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "TLS init failed" in connect_check.message


# ---------------------------------------------------------------------------
# Deep (L2) tests
# ---------------------------------------------------------------------------


class TestOpenSearchClientValidatorDeep:
    def test_happy_path_pass(self) -> None:
        # GIVEN a healthy cluster and successful canary operations
        validator = _make_validator(VALID_DATABAG)
        stub_client = OpenSearchClientStub()

        with (
            patch.object(OpenSearchClientValidator, "_build_client", return_value=stub_client),
            patch.object(OpenSearchClientValidator, "_remove_ca_file"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "PASS"
        assert result.level == "deep"
        assert any(c.name == "cluster_health" and c.passed for c in result.checks)
        assert any(c.name == "index_document" and c.passed for c in result.checks)
        assert any(c.name == "document_get" and c.passed for c in result.checks)
        assert any(c.name == "document_delete" and c.passed for c in result.checks)

    def test_fails_when_build_client_raises(self) -> None:
        # GIVEN _build_client() raises (e.g. bad TLS config)
        validator = _make_validator(VALID_DATABAG)

        with patch.object(OpenSearchClientValidator, "_build_client", side_effect=Exception("TLS init failed")):
            result = validator.validate(level="deep")

        # THEN the result is FAIL with a connect check — not an unhandled ERROR
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "TLS init failed" in connect_check.message

    def test_fails_when_index_missing_from_databag(self) -> None:
        # GIVEN databag has no 'index' key
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "index"}
        validator = _make_validator(databag)
        stub_client = OpenSearchClientStub()

        with (
            patch.object(OpenSearchClientValidator, "_build_client", return_value=stub_client),
            patch.object(OpenSearchClientValidator, "_remove_ca_file"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        doc_check = next(c for c in result.checks if c.name == "index_document")
        assert not doc_check.passed
        assert "No 'index'" in doc_check.message

    def test_fails_when_document_index_fails(self) -> None:
        # GIVEN indexing the document fails
        validator = _make_validator(VALID_DATABAG)
        stub_client = OpenSearchClientStub(index_error=Exception("write blocked"))

        with (
            patch.object(OpenSearchClientValidator, "_build_client", return_value=stub_client),
            patch.object(OpenSearchClientValidator, "_remove_ca_file"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        doc_check = next(c for c in result.checks if c.name == "index_document")
        assert not doc_check.passed

    def test_fails_when_document_get_returns_wrong_content(self) -> None:
        # GIVEN the retrieved document does not contain the expected canary field
        validator = _make_validator(VALID_DATABAG)
        stub_client = OpenSearchClientStub(get_response={"_source": {"canary": False}})

        with (
            patch.object(OpenSearchClientValidator, "_build_client", return_value=stub_client),
            patch.object(OpenSearchClientValidator, "_remove_ca_file"),
        ):
            result = validator.validate(level="deep")

        assert result.status == "FAIL"
        get_check = next(c for c in result.checks if c.name == "document_get")
        assert not get_check.passed

    def test_document_delete_always_runs(self) -> None:
        # GIVEN document retrieval fails — the canary document should still be deleted
        validator = _make_validator(VALID_DATABAG)
        stub_client = OpenSearchClientStub(get_error=Exception("get failed"))

        with (
            patch.object(OpenSearchClientValidator, "_build_client", return_value=stub_client),
            patch.object(OpenSearchClientValidator, "_remove_ca_file"),
        ):
            result = validator.validate(level="deep")

        # THEN delete ran and passed even though get step failed
        delete_check = next((c for c in result.checks if c.name == "document_delete"), None)
        assert delete_check is not None
        assert delete_check.passed

    def test_skipped_for_uat_level(self) -> None:
        # GIVEN a valid databag
        validator = _make_validator(VALID_DATABAG)

        result = validator.validate(level="uat")

        assert result.status == "SKIPPED"
        assert result.error is not None


# ---------------------------------------------------------------------------
# Persistence validator tests
# ---------------------------------------------------------------------------

PERSISTENCE_VALID_DATABAG: dict[str, str] = {
    "endpoints": "10.0.0.1:9200",
    "username": "test-user",
    "password": "test-pass",
    "index": "test-index",
}

# Scope token produced by _canary_scope_token() for the default _make_persistence_validator()
# args (model_uuid="11111111-1111-1111-1111-111111111111", relation_id=0, unit_name="app/0").
# Verified by direct computation against the same sha256(f"{model_uuid}:{relation_id}:{unit_name}")
# algorithm the validator uses, so tests can assert on document contents without re-deriving it.
TEST_SCOPE = "da7d88bc9ad4d4fd"


def _canary_doc(scope: str, marker: str, ref: int) -> dict[str, Any]:
    """Build a document matching what _write_canary_document() writes, for test seeding."""
    return {
        "validator_scope": scope,
        "validator_marker": marker,
        "validator_checkpoint_ref": ref,
        "validator_kind": _KIND_VALUE,
    }


class TestExactMatchFilter:
    def test_matches_document_regardless_of_keyword_multi_field_mapping(self) -> None:
        # GIVEN the granted credentials can't read the shared index's mapping (see the
        # persistence validator's class docstring), so whether a field was dynamically mapped as
        # plain "keyword" or as analyzed "text" with a ".keyword" multi-field is unknown.
        clause = _exact_match_filter("validator_scope", TEST_SCOPE)

        # THEN the clause matches a document whether the field is queried directly (as it would be
        # if mapped "keyword") or via its ".keyword" multi-field (as it would be if mapped "text")
        assert _query_matches(clause, {"validator_scope": TEST_SCOPE})
        # And it does not match a document with a different value in that field
        assert not _query_matches(clause, {"validator_scope": "some-other-value"})

    def test_kind_sentinel_is_a_single_analyzer_safe_token(self) -> None:
        # GIVEN a pre-existing index maps validator_kind as analyzed "text" with no ".keyword"
        # multi-field. _exact_match_filter()'s plain-field clause is then the only one that can
        # ever match, and a standard analyzer splits on separator characters (underscores,
        # hyphens, whitespace) - a multi-word sentinel value would be indexed as several terms and
        # could never satisfy a single `term` query for the whole string, silently matching zero
        # documents even though they exist. The sentinel must therefore contain none of those
        # characters.
        assert re.fullmatch(r"[a-z0-9]+", _KIND_VALUE)


class TestOpenSearchClientPersistenceValidatorRole:
    @pytest.mark.parametrize("role", [RelationRoleStub.provides, RelationRoleStub.peer])
    def test_prepare_raises_not_applicable_for_non_requires_role(self, role: RelationRoleStub) -> None:
        # GIVEN a validator on the non-requires side of the relation
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG, role=role)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.prepare()

    def test_checkpoint_raises_not_applicable_for_non_requires_role(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG, role=RelationRoleStub.provides)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

    def test_cleanup_raises_not_applicable_for_non_requires_role(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG, role=RelationRoleStub.provides)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.cleanup()


class TestOpenSearchClientPersistenceValidatorConnection:
    def test_prepare_raises_when_endpoints_is_blank(self) -> None:
        # GIVEN a databag with a present but blank "endpoints" field
        databag = {**PERSISTENCE_VALID_DATABAG, "endpoints": ""}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="endpoints"):
            validator.prepare()

    def test_checkpoint_raises_when_endpoints_is_missing(self) -> None:
        # GIVEN a databag missing the "endpoints" field entirely
        databag = {k: v for k, v in PERSISTENCE_VALID_DATABAG.items() if k != "endpoints"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="endpoints"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

    def test_prepare_raises_when_endpoints_parse_to_no_usable_hosts(self) -> None:
        # GIVEN an "endpoints" value that is non-blank (so it passes validate_schema() and the
        # raw-string check) but parses to zero usable hosts once split on commas - e.g. a bare
        # comma. opensearch-py silently treats an empty host list as localhost:9200 rather than
        # raising, which could otherwise make this connect to an unintended local service instead
        # of the related OpenSearch cluster.
        databag = {**PERSISTENCE_VALID_DATABAG, "endpoints": ", "}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="no usable hosts"):
            validator.prepare()

    def test_prepare_raises_when_credentials_missing(self) -> None:
        # GIVEN a databag missing username/password
        databag = {"endpoints": "10.0.0.1:9200", "index": "test-index"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError):
            validator.prepare()

    def test_prepare_raises_when_index_is_missing(self) -> None:
        # GIVEN a databag with valid credentials but no granted "index" - there is no separate
        # index this validator can create for itself (see the class docstring), so this must be
        # treated as a hard error rather than silently falling back to some default name.
        databag = {k: v for k, v in PERSISTENCE_VALID_DATABAG.items() if k != "index"}
        validator = _make_persistence_validator(databag)

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=PersistenceOpenSearchClientStub()):
            with pytest.raises(RuntimeError, match="index"):
                validator.prepare()


class TestOpenSearchClientPersistenceValidatorPrepare:
    def test_writes_canary_document_and_returns_state(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            state = validator.prepare()

        # THEN
        assert isinstance(state, PersistenceState)
        assert state.ref == 1
        assert state.token
        documents = list(client.indices["test-index"].documents.values())
        assert len(documents) == 1
        assert documents[0]["validator_marker"] == state.token
        assert documents[0]["validator_scope"] == TEST_SCOPE
        assert documents[0]["validator_checkpoint_ref"] == 1

    def test_generates_distinct_tokens_across_calls(self) -> None:
        # GIVEN prepare() is called twice against the same relation/unit scope
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            first = validator.prepare()
            second = validator.prepare()

        # THEN each run gets a fresh, distinct token
        assert first.token != second.token
        # And "drops and recreates": the first run's document was cleared before the second wrote
        # its own, so the shared index only ever holds the latest run's canary document.
        documents = list(client.indices["test-index"].documents.values())
        assert len(documents) == 1
        assert documents[0]["validator_marker"] == second.token

    def test_idempotent_when_run_repeatedly(self) -> None:
        # GIVEN prepare() is called twice - e.g. a re-run of a failed prepare(), or leftover state
        # from a previous process
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            validator.prepare()
            second = validator.prepare()

            # WHEN checkpoint() is run against the second state
            result, _ = validator.checkpoint(second)

        # THEN it passes, proving the canary document left behind by the second prepare() is usable
        assert result.status == "PASS"

    def test_only_clears_documents_scoped_to_this_relation_and_unit(self) -> None:
        # GIVEN the shared index already holds a document belonging to a different relation/unit
        # scope, and the application's own (unrelated) data
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        other_scope_doc = _canary_doc("other-scope", "x", 1)
        app_doc = {"some_app_field": "some_app_value"}
        client.indices.setdefault("test-index", PersistenceIndexStub()).add(other_scope_doc)
        client.indices["test-index"].add(app_doc)

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            validator.prepare()

        # THEN the unrelated documents survive
        documents = list(client.indices["test-index"].documents.values())
        assert other_scope_doc in documents
        assert app_doc in documents


class TestOpenSearchClientPersistenceValidatorCheckpoint:
    def _seed(self, client: PersistenceOpenSearchClientStub, documents: list[dict[str, Any]]) -> None:
        store = client.indices.setdefault("test-index", PersistenceIndexStub())
        for doc in documents:
            store.add(doc)

    def test_passes_when_document_count_matches_expected_ref(self) -> None:
        # GIVEN the shared index has exactly the expected number of matching documents
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        self._seed(
            client,
            [
                _canary_doc(TEST_SCOPE, TEST_TOKEN, 1),
                _canary_doc(TEST_SCOPE, TEST_TOKEN, 2),
            ],
        )

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN
        assert result.status == "PASS"
        check = next(c for c in result.checks if c.name == "document_count")
        assert check.passed
        assert new_state.id == 42
        assert new_state.ref == 3
        assert new_state.token == TEST_TOKEN
        # And a new document was indexed, carrying the same token forward
        documents = list(client.indices["test-index"].documents.values())
        assert len(documents) == 3
        assert any(doc["validator_checkpoint_ref"] == 3 and doc["validator_marker"] == TEST_TOKEN for doc in documents)

    def test_fails_without_raising_when_document_count_does_not_match(self) -> None:
        # GIVEN the shared index has fewer matching documents than expected
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        self._seed(
            client,
            [_canary_doc(TEST_SCOPE, TEST_TOKEN, 1)],
        )

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN a FAIL result is returned, not an exception, and no write occurred / state advanced
        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "document_count")
        assert not check.passed
        assert new_state.id == 42
        assert new_state.ref == 2
        assert len(client.indices["test-index"].documents) == 1

    def test_fails_when_a_same_token_ref_is_duplicated_alongside_a_missing_one(self) -> None:
        # GIVEN the index has 2 matching documents (same total count as expected), but they're a
        # duplicate ref=1 and a missing ref=2, rather than the genuine [1, 2] sequence: a bare
        # `count() == expected.ref` check would wrongly accept this.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        self._seed(
            client,
            [
                _canary_doc(TEST_SCOPE, TEST_TOKEN, 1),
                _canary_doc(TEST_SCOPE, TEST_TOKEN, 1),
            ],
        )

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN the duplicate is not mistaken for the missing ref 2
        assert result.status == "FAIL"
        assert new_state.ref == 2
        assert len(client.indices["test-index"].documents) == 2

    def test_fails_when_a_same_token_document_has_a_malformed_ref(self) -> None:
        # GIVEN a same-token document whose validator_checkpoint_ref isn't a real int (e.g.
        # corrupted data): filtering malformed refs out up front would let it be silently ignored.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        self._seed(
            client,
            [
                {
                    "validator_scope": TEST_SCOPE,
                    "validator_marker": TEST_TOKEN,
                    "validator_checkpoint_ref": "1",
                    "validator_kind": _KIND_VALUE,
                }
            ],
        )

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=1))

        # THEN the malformed ref correctly fails the check instead of being silently dropped
        assert result.status == "FAIL"
        assert new_state.ref == 1

    def test_reads_matching_refs_across_multiple_search_pages(self) -> None:
        # GIVEN more matching documents than fit in a single search page: only inspecting the
        # first page would under-count them and could report a false FAIL.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        self._seed(client, [_canary_doc(TEST_SCOPE, TEST_TOKEN, ref) for ref in range(1, 6)])

        with (
            patch("validators.opensearch_client.validator.OpenSearch", return_value=client),
            patch("validators.opensearch_client.validator._CLEANUP_SEARCH_SIZE", 2),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=5))

        # THEN all 5 documents across 3 pages were found and verified
        assert result.status == "PASS"
        assert new_state.ref == 6

    def test_fails_when_matching_token_belongs_to_a_different_scope(self) -> None:
        # GIVEN the shared index has documents carrying this expected token, but tagged with a
        # different relation/unit's validator_scope - e.g. a token collision, or a state that was
        # somehow copied across relations. These must not count towards this scope's expected
        # ref: doing so would let another scope's data produce a false PASS here.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        self._seed(
            client,
            [
                _canary_doc("other-scope", TEST_TOKEN, 1),
                _canary_doc("other-scope", TEST_TOKEN, 2),
            ],
        )

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN a FAIL result is returned (0 documents found in this scope), not a false PASS
        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "document_count")
        assert not check.passed
        assert new_state.ref == 2
        # And the other scope's documents were left untouched, with no new document written
        assert len(client.indices["test-index"].documents) == 2

    def test_fails_when_matching_scope_and_marker_belong_to_a_non_canary_document(self) -> None:
        # GIVEN an application document that happens to carry matching validator_scope and
        # validator_marker field names/values, but lacks validator_kind - i.e. it is not actually
        # a canary document this validator wrote, so it must not satisfy the expected count.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        self._seed(
            client,
            [{"validator_scope": TEST_SCOPE, "validator_marker": TEST_TOKEN, "some_app_field": "some_app_value"}],
        )

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=1))

        # THEN a FAIL result is returned (0 genuine canary documents found), not a false PASS
        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "document_count")
        assert not check.passed
        assert new_state.ref == 1

    def test_fails_when_index_does_not_exist(self) -> None:
        # GIVEN the shared index does not exist yet (e.g. never granted / relation still forming)
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=1))

        # THEN
        assert result.status == "FAIL"
        assert new_state.ref == 1

    def test_fails_when_data_recreated_from_scratch_with_same_count_but_different_token(self) -> None:
        # GIVEN the shared index holds the same number of documents, but tagged with a different
        # (unmatched) marker token - a bare count-only check would pass this falsely
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        self._seed(
            client,
            [_canary_doc(TEST_SCOPE, "a-different-token", 1)],
        )

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=1))

        # THEN it FAILs rather than reporting a false PASS
        assert result.status == "FAIL"
        assert new_state.ref == 1

    def test_raises_when_expected_ref_is_out_of_range(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)

        # WHEN / THEN
        with pytest.raises(ValueError, match="out of range"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=0))

    def test_raises_when_expected_identifier_is_out_of_range(self) -> None:
        # GIVEN an identifier larger than any prepare() could have produced (> 63 bits)
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)

        # WHEN / THEN
        with pytest.raises(ValueError, match="out of range"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=(1 << 63), ref=1))

    def test_raises_when_expected_identifier_is_negative(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)

        # WHEN / THEN
        with pytest.raises(ValueError, match="out of range"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=-1, ref=1))

    def test_raises_when_expected_token_is_empty(self) -> None:
        # GIVEN a restored/malformed state with no token (can't have come from a real prepare())
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)

        # WHEN / THEN
        with pytest.raises(ValueError, match="token"):
            validator.checkpoint(PersistenceState(token="", id=1, ref=1))


class TestOpenSearchClientPersistenceValidatorCleanup:
    def test_deletes_all_canary_documents_for_this_scope(self) -> None:
        # GIVEN two leftover canary documents belonging to this relation/unit scope
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        store = client.indices.setdefault("test-index", PersistenceIndexStub())
        doc_1 = store.add(_canary_doc(TEST_SCOPE, "a", 1))
        doc_2 = store.add(_canary_doc(TEST_SCOPE, "b", 2))

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            validator.cleanup()

        # THEN
        assert doc_1 not in store.documents
        assert doc_2 not in store.documents

    def test_deletes_all_documents_across_multiple_search_pages(self) -> None:
        # GIVEN more scoped canary documents than fit in a single cleanup() search page - each
        # `delete()` call uses refresh=true, so a deleted document must not reappear in the next
        # page's search, and cleanup() must keep paging until no scoped hits remain instead of
        # only ever clearing the first page.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        store = client.indices.setdefault("test-index", PersistenceIndexStub())
        doc_ids = [store.add(_canary_doc(TEST_SCOPE, str(i), 1)) for i in range(5)]

        with (
            patch("validators.opensearch_client.validator.OpenSearch", return_value=client),
            patch("validators.opensearch_client.validator._CLEANUP_SEARCH_SIZE", 2),
        ):
            # WHEN
            validator.cleanup()

        # THEN every scoped document was removed, not just the first page's worth
        for doc_id in doc_ids:
            assert doc_id not in store.documents

    def test_is_a_noop_when_no_canary_documents_exist(self) -> None:
        # GIVEN no canary documents exist (index not yet created)
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN / THEN (must not raise)
            validator.cleanup()

    def test_leaves_other_scopes_and_application_data_untouched(self) -> None:
        # GIVEN the shared index holds a document from a different relation/unit scope and the
        # target application's own (unrelated) data, alongside this scope's canary document
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        store = client.indices.setdefault("test-index", PersistenceIndexStub())
        store.add(_canary_doc(TEST_SCOPE, "a", 1))
        other_scope_id = store.add(_canary_doc("other-scope", "x", 1))
        app_doc_id = store.add({"some_app_field": "some_app_value"})

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            validator.cleanup()

        # THEN only this scope's canary document is gone
        assert other_scope_id in store.documents
        assert app_doc_id in store.documents
        assert not any(doc.get("validator_scope") == TEST_SCOPE for doc in store.documents.values())

    def test_leaves_application_document_untouched_even_with_a_matching_scope_value(self) -> None:
        # GIVEN an application document that happens to carry the same field name/value as this
        # validator's validator_scope tag, but lacks validator_kind (i.e. it is not actually a
        # canary document this validator wrote). Ownership must be established by validator_kind,
        # not by validator_scope alone, or genuine application data could be deleted.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        client = PersistenceOpenSearchClientStub()
        store = client.indices.setdefault("test-index", PersistenceIndexStub())
        lookalike_app_doc_id = store.add({"validator_scope": TEST_SCOPE, "some_app_field": "some_app_value"})
        genuine_canary_id = store.add(_canary_doc(TEST_SCOPE, "a", 1))

        with patch("validators.opensearch_client.validator.OpenSearch", return_value=client):
            # WHEN
            validator.cleanup()

        # THEN only the genuine, validator_kind-tagged canary document is gone
        assert lookalike_app_doc_id in store.documents
        assert genuine_canary_id not in store.documents

    def test_raises_not_applicable_when_credentials_incomplete(self) -> None:
        # GIVEN a databag with incomplete credentials (relation still being set up)
        databag = {"endpoints": "10.0.0.1:9200", "index": "test-index"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.cleanup()
