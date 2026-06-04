# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
from typing import cast
from unittest.mock import MagicMock, patch

import ops
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from validators.s3.validator import S3Validator
from validators.test_utils.helpers import make_charm_from_relation, make_charm_from_relation_and_secrets
from validators.test_utils.stubs import ApplicationStub, RelationRoleStub, RelationStub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_validator(
    databag: dict[str, str], endpoint: str = "s3", role: RelationRoleStub = RelationRoleStub.requires
) -> S3Validator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    charm = make_charm_from_relation(relation, interface_name="s3", role=role)
    return S3Validator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "error"}}, "HeadBucket")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "bucket": "my-bucket",
    "access-key": "test-access-key",
    "secret-key": "test-secret-key",
    "endpoint": "http://s3.example.com",
    "region": "us-east-1",
}

# ---------------------------------------------------------------------------
# Tests: simple level
# ---------------------------------------------------------------------------


class TestS3ValidatorSimple:
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

    def test_returns_skipped_for_uat_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None
        assert "not supported" in result.error

    def test_returns_error_when_relation_app_not_in_scope(self) -> None:
        # GIVEN a relation whose remote app has not yet joined
        app = ApplicationStub()
        relation = RelationStub(name="s3", id=0, app=app)
        relation.data = {}  # remove app added by __post_init__ to simulate not-in-scope
        charm = make_charm_from_relation(relation, interface_name="s3")
        validator = S3Validator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"

    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        # GIVEN a databag missing secret-key
        validator = _make_validator({"bucket": "my-bucket", "access-key": "test-access-key"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "secret-key" in schema_check.message

    def test_passes_when_bucket_is_accessible(self) -> None:
        # GIVEN a valid databag and a mock S3 client that succeeds head_bucket
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}

        with patch("validators.s3.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        bucket_check = next(c for c in result.checks if c.name == "bucket_accessible")
        assert bucket_check.passed

    def test_fails_when_bucket_not_found(self) -> None:
        # GIVEN head_bucket raises a 404 ClientError
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.side_effect = _client_error("404")

        with patch("validators.s3.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        bucket_check = next(c for c in result.checks if c.name == "bucket_accessible")
        assert not bucket_check.passed
        assert "404" in bucket_check.message

    def test_fails_when_bucket_access_denied(self) -> None:
        # GIVEN head_bucket raises a 403 ClientError
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.side_effect = _client_error("403")

        with patch("validators.s3.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        bucket_check = next(c for c in result.checks if c.name == "bucket_accessible")
        assert not bucket_check.passed

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-s3")
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}

        with patch("validators.s3.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-s3"
        assert result.interface == "s3"

    def test_uses_path_addressing_style_by_default(self) -> None:
        # GIVEN no s3-uri-style in databag
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}

        with patch("validators.s3.validator.boto3.client", return_value=mock_client) as mock_boto:
            validator.validate(level="simple")

        call_kwargs = mock_boto.call_args.kwargs
        assert call_kwargs["config"].s3["addressing_style"] == "path"

    def test_tls_ca_chain_written_and_cleaned_up(self) -> None:
        # GIVEN a databag with tls-ca-chain set
        ca_databag = {
            **VALID_DATABAG,
            "tls-ca-chain": "-----BEGIN CERTIFICATE-----\nfake-ca\n-----END CERTIFICATE-----\n",
        }
        validator = _make_validator(ca_databag)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}

        written_path: list[str] = []

        def capture_verify(*args: object, **kwargs: object) -> MagicMock:
            written_path.append(str(kwargs.get("verify", "")))
            return mock_client

        with patch("validators.s3.validator.boto3.client", side_effect=capture_verify):
            result = validator.validate(level="simple")

        # THEN boto3.client was called with verify= pointing to a temp .pem file
        assert result.status == "PASS"
        assert len(written_path) == 1
        assert written_path[0].endswith(".pem")

        # AND the temp file has been cleaned up after validation
        assert not os.path.exists(written_path[0])

    def test_passes_with_secret_backed_credentials(self) -> None:
        # GIVEN a databag whose credentials are behind a Juju secret URI
        secret_uri = "secret:abc123"
        secret_content = {"access-key": "test-access-key", "secret-key": "test-secret-key"}
        databag = {
            "bucket": "my-bucket",
            "endpoint": "http://s3.example.com",
            "region": "us-east-1",
            "s3-credentials": secret_uri,
        }
        app = ApplicationStub()
        relation = RelationStub(name="s3", id=0, app=app, data={app: databag})
        charm = make_charm_from_relation_and_secrets(relation, {secret_uri: secret_content})
        validator = S3Validator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}

        with patch("validators.s3.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="simple")

        # THEN credentials are resolved from the secret and validation passes
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed


# ---------------------------------------------------------------------------
# Tests: deep level
# ---------------------------------------------------------------------------


class TestS3ValidatorDeep:
    def test_deep_passes_on_successful_write_read_delete(self) -> None:
        # GIVEN a complete databag and mock client that succeeds all operations
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}
        mock_client.put_object.return_value = {}
        mock_client.get_object.return_value = {"Body": MagicMock(read=lambda: b"s3-validator-canary")}
        mock_client.delete_object.return_value = {}

        with patch("validators.s3.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        assert result.level == "deep"
        assert next(c for c in result.checks if c.name == "write").passed
        assert next(c for c in result.checks if c.name == "read_verify").passed
        assert next(c for c in result.checks if c.name == "cleanup").passed

    def test_deep_fails_when_write_fails(self) -> None:
        # GIVEN put_object raises an error
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}
        mock_client.put_object.side_effect = _client_error("AccessDenied")

        with patch("validators.s3.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        write_check = next(c for c in result.checks if c.name == "write")
        assert not write_check.passed
        # AND cleanup was still attempted even though write failed
        mock_client.delete_object.assert_called_once()
        cleanup_check = next(c for c in result.checks if c.name == "cleanup")
        assert cleanup_check.passed

    def test_deep_fails_when_read_body_mismatches(self) -> None:
        # GIVEN get_object returns wrong body
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}
        mock_client.put_object.return_value = {}
        mock_client.get_object.return_value = {"Body": MagicMock(read=lambda: b"wrong-body")}
        mock_client.delete_object.return_value = {}

        with patch("validators.s3.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        read_check = next(c for c in result.checks if c.name == "read_verify")
        assert not read_check.passed
        assert "mismatch" in read_check.message

    def test_deep_fails_when_bucket_not_accessible(self) -> None:
        # GIVEN head_bucket returns 403
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.side_effect = _client_error("403")

        with patch("validators.s3.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        bucket_check = next(c for c in result.checks if c.name == "bucket_accessible")
        assert not bucket_check.passed

    def test_deep_returns_skipped_for_uat(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"

    def test_deep_cleanup_still_runs_after_read_failure(self) -> None:
        # GIVEN get_object raises an error
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}
        mock_client.put_object.return_value = {}
        mock_client.get_object.side_effect = _client_error("InternalError")
        mock_client.delete_object.return_value = {}

        with patch("validators.s3.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="deep")

        # THEN cleanup still ran
        mock_client.delete_object.assert_called_once()
        cleanup_check = next(c for c in result.checks if c.name == "cleanup")
        assert cleanup_check.passed

    def test_deep_uses_path_prefix_for_canary_key(self) -> None:
        # GIVEN databag includes a path prefix
        databag = {**VALID_DATABAG, "path": "data/backups"}
        validator = _make_validator(databag)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}
        mock_client.put_object.return_value = {}
        mock_client.get_object.return_value = {"Body": MagicMock(read=lambda: b"s3-validator-canary")}
        mock_client.delete_object.return_value = {}

        with patch("validators.s3.validator.boto3.client", return_value=mock_client):
            validator.validate(level="deep")

        put_call_kwargs = mock_client.put_object.call_args.kwargs
        assert put_call_kwargs["Key"].startswith("data/backups/")
