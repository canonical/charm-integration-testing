# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import pytest

from validators.opensearch_client.validator import OpenSearchClientValidator
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
