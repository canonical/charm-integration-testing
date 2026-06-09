# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import cast
from unittest.mock import MagicMock, patch

import ops
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from validators.object_storage.validator import ObjectStorageValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import ApplicationStub, RelationRoleStub, RelationStub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_validator(
    databag: dict[str, str],
    endpoint: str = "object-storage",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> ObjectStorageValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    charm = make_charm_from_relation(relation, interface_name="object-storage", role=role)
    return ObjectStorageValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "error"}}, "HeadBucket")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "endpoint": "http://storage.example.com",
    "bucket": "my-bucket",
    "access-key": "test-access-key",
    "secret-key": "test-secret-key",
    "region": "us-east-1",
}

# ---------------------------------------------------------------------------
# Tests: simple level
# ---------------------------------------------------------------------------


class TestObjectStorageValidatorSimple:
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
        relation = RelationStub(name="object-storage", id=0, app=app)
        relation.data = {}
        charm = make_charm_from_relation(relation, interface_name="object-storage")
        validator = ObjectStorageValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"

    @pytest.mark.parametrize("missing_field", ["endpoint", "bucket", "access-key", "secret-key"])
    def test_fails_schema_check_when_required_field_missing(self, missing_field: str) -> None:
        # GIVEN a databag missing one required field
        databag = {k: v for k, v in VALID_DATABAG.items() if k != missing_field}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert missing_field in schema_check.message

    def test_passes_when_bucket_is_accessible(self) -> None:
        # GIVEN a valid databag and a mock S3 client that succeeds head_bucket
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client):
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

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client):
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

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        bucket_check = next(c for c in result.checks if c.name == "bucket_accessible")
        assert not bucket_check.passed

    def test_passes_without_optional_region(self) -> None:
        # GIVEN a databag without the optional region field
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "region"}
        validator = _make_validator(databag)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client) as mock_boto:
            result = validator.validate(level="simple")

        # THEN validation passes and a default region is used
        assert result.status == "PASS"
        call_kwargs = mock_boto.call_args.kwargs
        assert call_kwargs["region_name"] == "us-east-1"

    def test_uses_region_from_databag_when_provided(self) -> None:
        # GIVEN a databag with an explicit region
        databag = {**VALID_DATABAG, "region": "eu-west-1"}
        validator = _make_validator(databag)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client) as mock_boto:
            validator.validate(level="simple")

        call_kwargs = mock_boto.call_args.kwargs
        assert call_kwargs["region_name"] == "eu-west-1"

    def test_endpoint_and_interface_set_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-object-storage")
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-object-storage"
        assert result.interface == "object-storage"

    def test_uses_path_addressing_style(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client) as mock_boto:
            validator.validate(level="simple")

        call_kwargs = mock_boto.call_args.kwargs
        assert call_kwargs["config"].s3["addressing_style"] == "path"

    def test_endpoint_url_passed_to_client(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client) as mock_boto:
            validator.validate(level="simple")

        call_kwargs = mock_boto.call_args.kwargs
        assert call_kwargs["endpoint_url"] == "http://storage.example.com"


# ---------------------------------------------------------------------------
# Tests: deep level
# ---------------------------------------------------------------------------


class TestObjectStorageValidatorDeep:
    def test_deep_passes_on_successful_write_read_delete(self) -> None:
        # GIVEN a complete databag and mock client that succeeds all operations
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}
        mock_client.put_object.return_value = {}
        mock_client.get_object.return_value = {"Body": MagicMock(read=lambda: b"object-storage-validator-canary")}
        mock_client.delete_object.return_value = {}

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client):
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

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        write_check = next(c for c in result.checks if c.name == "write")
        assert not write_check.passed
        # AND cleanup was still attempted
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

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        read_check = next(c for c in result.checks if c.name == "read_verify")
        assert not read_check.passed
        assert "mismatch" in read_check.message

    def test_deep_fails_when_bucket_not_accessible(self) -> None:
        # GIVEN head_bucket raises 403
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.side_effect = _client_error("403")

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        bucket_check = next(c for c in result.checks if c.name == "bucket_accessible")
        assert not bucket_check.passed
        # AND no write/read/cleanup was attempted
        mock_client.put_object.assert_not_called()

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

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client):
            result = validator.validate(level="deep")

        # THEN cleanup still ran
        mock_client.delete_object.assert_called_once()
        cleanup_check = next(c for c in result.checks if c.name == "cleanup")
        assert cleanup_check.passed

    def test_deep_canary_key_uses_unique_prefix(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)
        mock_client = MagicMock()
        mock_client.head_bucket.return_value = {}
        mock_client.put_object.return_value = {}
        mock_client.get_object.return_value = {"Body": MagicMock(read=lambda: b"object-storage-validator-canary")}
        mock_client.delete_object.return_value = {}

        with patch("validators.object_storage.validator.boto3.client", return_value=mock_client):
            validator.validate(level="deep")

        put_call_kwargs = mock_client.put_object.call_args.kwargs
        assert put_call_kwargs["Key"].startswith("__canary_")
