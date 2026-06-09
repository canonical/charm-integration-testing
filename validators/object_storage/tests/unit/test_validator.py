# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from typing import cast
from unittest.mock import MagicMock, patch

import ops
import pytest
import yaml
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from validators.object_storage.validator import ObjectStorageValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import ApplicationStub, RelationRoleStub, RelationStub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_SDI_DATA: dict[str, str] = {
    "service": "minio",
    "namespace": "test-model",
    "port": "9000",
    "access-key": "miniouser",
    "secret-key": "miniopassword",
    "secure": "false",
}

VALID_DATABAG: dict[str, str] = {
    "_supported_versions": "- v1\n",
    "data": yaml.dump(_VALID_SDI_DATA),
}


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
    return ClientError({"Error": {"Code": code, "Message": "error"}}, "Operation")


def _mock_client(
    create_bucket: ClientError | None = None,
    delete_bucket: ClientError | None = None,
    put_object: ClientError | None = None,
    get_object_rv: dict[str, object] | None = None,
    get_object_se: ClientError | None = None,
    delete_object: ClientError | None = None,
) -> MagicMock:
    """Return a mock S3 client with configurable side effects / return values."""
    client = MagicMock()
    if create_bucket is not None:
        client.create_bucket.side_effect = create_bucket
    else:
        client.create_bucket.return_value = {}
    if delete_bucket is not None:
        client.delete_bucket.side_effect = delete_bucket
    else:
        client.delete_bucket.return_value = {}
    if put_object is not None:
        client.put_object.side_effect = put_object
    else:
        client.put_object.return_value = {}
    if get_object_se is not None:
        client.get_object.side_effect = get_object_se
    elif get_object_rv is not None:
        client.get_object.return_value = get_object_rv
    else:
        body_mock = MagicMock(read=lambda: b"object-storage-validator-canary")
        client.get_object.return_value = {"Body": body_mock}
    if delete_object is not None:
        client.delete_object.side_effect = delete_object
    else:
        client.delete_object.return_value = {}
    return client


# ---------------------------------------------------------------------------
# Tests: role / level gating
# ---------------------------------------------------------------------------


class TestObjectStorageValidatorGating:
    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.requires, False),
            (RelationRoleStub.provides, True),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_returns_skipped_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, role=role)

        # WHEN
        with patch("validators.object_storage.validator.boto3.client", return_value=_mock_client()):
            result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

    def test_returns_skipped_for_uat_level(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        result = validator.validate(level="uat")
        assert result.status == "SKIPPED"
        assert result.error is not None and "not supported" in result.error

    def test_returns_error_when_relation_app_not_in_scope(self) -> None:
        app = ApplicationStub()
        relation = RelationStub(name="object-storage", id=0, app=app)
        relation.data = {}
        charm = make_charm_from_relation(relation, interface_name="object-storage")
        validator = ObjectStorageValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))
        result = validator.validate(level="simple")
        assert result.status == "ERROR"


# ---------------------------------------------------------------------------
# Tests: schema / SDI parsing
# ---------------------------------------------------------------------------


class TestObjectStorageValidatorSchema:
    @pytest.mark.parametrize(
        "missing_field",
        ["service", "namespace", "port", "access-key", "secret-key"],
    )
    def test_fails_when_sdi_field_missing(self, missing_field: str) -> None:
        sdi = {k: v for k, v in _VALID_SDI_DATA.items() if k != missing_field}
        databag = {"_supported_versions": "- v1\n", "data": yaml.dump(sdi)}
        validator = _make_validator(databag)
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert missing_field in schema_check.message

    def test_fails_when_data_key_absent(self) -> None:
        validator = _make_validator({"_supported_versions": "- v1\n"})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        assert not next(c for c in result.checks if c.name == "schema").passed

    def test_fails_when_databag_is_empty(self) -> None:
        validator = _make_validator({})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"

    def test_fails_when_sdi_data_is_invalid_yaml(self) -> None:
        validator = _make_validator({"_supported_versions": "- v1\n", "data": "[invalid"})
        result = validator.validate(level="simple")
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed


# ---------------------------------------------------------------------------
# Tests: simple level
# ---------------------------------------------------------------------------


class TestObjectStorageValidatorSimple:
    def test_passes_when_bucket_created_and_deleted(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.object_storage.validator.boto3.client", return_value=_mock_client()):
            result = validator.validate(level="simple")
        assert result.status == "PASS"
        assert next(c for c in result.checks if c.name == "bucket_create").passed
        assert next(c for c in result.checks if c.name == "bucket_cleanup").passed

    def test_fails_when_create_bucket_raises_client_error(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        client = _mock_client(create_bucket=_client_error("AccessDenied"))
        with patch("validators.object_storage.validator.boto3.client", return_value=client):
            result = validator.validate(level="simple")
        assert result.status == "FAIL"
        assert not next(c for c in result.checks if c.name == "bucket_create").passed
        # bucket_cleanup should NOT appear when bucket was never created
        assert not any(c.name == "bucket_cleanup" for c in result.checks)

    def test_endpoint_and_interface_set_on_result(self) -> None:
        validator = _make_validator(VALID_DATABAG, endpoint="my-object-storage")
        with patch("validators.object_storage.validator.boto3.client", return_value=_mock_client()):
            result = validator.validate(level="simple")
        assert result.endpoint == "my-object-storage"
        assert result.interface == "object-storage"

    def test_endpoint_url_built_from_sdi_fields(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.object_storage.validator.boto3.client", return_value=_mock_client()) as mock_boto:
            validator.validate(level="simple")
        call_kwargs = mock_boto.call_args.kwargs
        assert call_kwargs["endpoint_url"] == "http://minio.test-model.svc.cluster.local:9000"

    def test_endpoint_uses_https_when_secure_is_true(self) -> None:
        sdi = {**_VALID_SDI_DATA, "secure": "true"}
        databag = {"_supported_versions": "- v1\n", "data": yaml.dump(sdi)}
        validator = _make_validator(databag)
        with patch("validators.object_storage.validator.boto3.client", return_value=_mock_client()) as mock_boto:
            validator.validate(level="simple")
        assert mock_boto.call_args.kwargs["endpoint_url"].startswith("https://")

    def test_uses_path_addressing_style(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.object_storage.validator.boto3.client", return_value=_mock_client()) as mock_boto:
            validator.validate(level="simple")
        assert mock_boto.call_args.kwargs["config"].s3["addressing_style"] == "path"

    def test_bucket_name_uses_validator_prefix(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.object_storage.validator.boto3.client", return_value=_mock_client()) as mock_boto:
            validator.validate(level="simple")
        bucket = mock_boto.return_value.create_bucket.call_args.kwargs["Bucket"]
        assert bucket.startswith("validator-")


# ---------------------------------------------------------------------------
# Tests: deep level
# ---------------------------------------------------------------------------


class TestObjectStorageValidatorDeep:
    def test_passes_on_successful_write_read_delete(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.object_storage.validator.boto3.client", return_value=_mock_client()):
            result = validator.validate(level="deep")
        assert result.status == "PASS"
        assert next(c for c in result.checks if c.name == "write").passed
        assert next(c for c in result.checks if c.name == "read_verify").passed
        assert next(c for c in result.checks if c.name == "canary_cleanup").passed
        assert next(c for c in result.checks if c.name == "bucket_cleanup").passed

    def test_fails_when_write_fails_and_cleanup_still_runs(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        client = _mock_client(put_object=_client_error("AccessDenied"))
        with patch("validators.object_storage.validator.boto3.client", return_value=client):
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        assert not next(c for c in result.checks if c.name == "write").passed
        client.delete_object.assert_called_once()
        client.delete_bucket.assert_called_once()

    def test_fails_when_read_body_mismatches(self) -> None:
        body_mock = MagicMock(read=lambda: b"wrong-data")
        mc = _mock_client(get_object_rv={"Body": body_mock})
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.object_storage.validator.boto3.client", return_value=mc):
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        read_check = next(c for c in result.checks if c.name == "read_verify")
        assert not read_check.passed
        assert "mismatch" in read_check.message

    def test_cleanup_runs_after_read_failure(self) -> None:
        mc = _mock_client(get_object_se=_client_error("InternalError"))
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.object_storage.validator.boto3.client", return_value=mc):
            validator.validate(level="deep")
        mc.delete_object.assert_called_once()
        mc.delete_bucket.assert_called_once()

    def test_bucket_not_created_when_create_fails(self) -> None:
        mc = _mock_client(create_bucket=_client_error("AccessDenied"))
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.object_storage.validator.boto3.client", return_value=mc):
            result = validator.validate(level="deep")
        assert result.status == "FAIL"
        mc.put_object.assert_not_called()
        mc.delete_bucket.assert_not_called()

    def test_deep_skipped_for_uat(self) -> None:
        validator = _make_validator(VALID_DATABAG)
        result = validator.validate(level="uat")
        assert result.status == "SKIPPED"

    def test_canary_key_uses_unique_prefix(self) -> None:
        mc = _mock_client()
        validator = _make_validator(VALID_DATABAG)
        with patch("validators.object_storage.validator.boto3.client", return_value=mc):
            validator.validate(level="deep")
        assert mc.put_object.call_args.kwargs["Key"].startswith("__canary_")
