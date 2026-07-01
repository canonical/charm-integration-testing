 # Copyright 2026 Canonical Ltd.
 # See LICENSE file for licensing details.

from typing import cast

import ops
import pytest

from validators.base import ValidationLevel
from validators.azure_storage.validator import AzureStorageValidator
from validators.test_utils.helpers import make_charm_from_relation, make_charm_from_relation_and_secrets
from validators.test_utils.stubs import ApplicationStub, RelationRoleStub, RelationStub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "container": "my-container",
    "storage-account": "mystorageaccount",
    "secret-key": "hunter2",
    "connection-protocol": "abfss",
    "endpoint": "abfss://my-container@mystorageaccount.dfs.core.windows.net/",
}


def _make_validator(
    databag: dict[str, str],
    endpoint: str = "azure-storage-credentials",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> AzureStorageValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    charm = make_charm_from_relation(relation, interface_name="azure_storage", role=role)
    return AzureStorageValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


# ---------------------------------------------------------------------------
# Role filtering
# ---------------------------------------------------------------------------


class TestAzureStorageValidatorRole:
    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.requires, False),
            (RelationRoleStub.provides, True),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_role_filtering(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, role=role)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip


# ---------------------------------------------------------------------------
# Unsupported level
# ---------------------------------------------------------------------------


class TestAzureStorageValidatorLevel:
    @pytest.mark.parametrize("level", ["deep", "uat"])
    def test_unsupported_levels_return_skipped(self, level: ValidationLevel) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level=level)

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None
        assert "not supported" in result.error


# ---------------------------------------------------------------------------
# No remote app
# ---------------------------------------------------------------------------


class TestAzureStorageValidatorNoApp:
    def test_returns_error_when_remote_app_not_in_scope(self) -> None:
        # GIVEN a relation whose remote app has not yet joined
        app = ApplicationStub()
        relation = RelationStub(name="azure-storage-credentials", id=0, app=app)
        relation.data = {}  # simulate app not yet in scope
        charm = make_charm_from_relation(relation, interface_name="azure_storage")
        validator = AzureStorageValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"


# ---------------------------------------------------------------------------
# Simple: schema checks
# ---------------------------------------------------------------------------


class TestAzureStorageValidatorSimple:
    def test_passes_with_all_required_fields(self) -> None:
        # GIVEN a complete databag
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed

    def test_fails_when_container_missing(self) -> None:
        # GIVEN
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "container"}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "container" in schema_check.message

    def test_fails_when_storage_account_missing(self) -> None:
        # GIVEN
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "storage-account"}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "storage-account" in schema_check.message

    def test_fails_when_secret_key_missing(self) -> None:
        # GIVEN
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "secret-key"}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "secret-key" in schema_check.message

    def test_passes_with_secret_backed_credentials(self) -> None:
        # GIVEN secret-key is exposed via a Juju secret URI in the 'secret-extra' field
        secret_uri = "secret:abc123"
        secret_content = {"secret-key": "supersecret"}
        databag = {
            "container": "my-container",
            "storage-account": "mystorageaccount",
            "secret-extra": secret_uri,
        }
        app = ApplicationStub()
        relation = RelationStub(name="azure-storage-credentials", id=0, app=app, data={app: databag})
        charm = make_charm_from_relation_and_secrets(relation, {secret_uri: secret_content})
        validator = AzureStorageValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed

    def test_passes_without_optional_fields(self) -> None:
        # GIVEN only the three required fields present
        databag = {
            "container": "my-container",
            "storage-account": "mystorageaccount",
            "secret-key": "hunter2",
        }
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"

    # ------------------------------------------------------------------
    # connection-protocol checks
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("protocol", ["wasb", "wasbs", "abfs", "abfss", "http", "https"])
    def test_passes_with_valid_connection_protocol(self, protocol: str) -> None:
        # GIVEN
        databag = {**VALID_DATABAG, "connection-protocol": protocol}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        protocol_check = next(c for c in result.checks if c.name == "connection_protocol")
        assert protocol_check.passed

    def test_fails_with_invalid_connection_protocol(self) -> None:
        # GIVEN
        databag = {**VALID_DATABAG, "connection-protocol": "ftp"}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        protocol_check = next(c for c in result.checks if c.name == "connection_protocol")
        assert not protocol_check.passed
        assert "ftp" in protocol_check.message

    def test_passes_without_connection_protocol(self) -> None:
        # GIVEN no connection-protocol field
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "connection-protocol"}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        protocol_check = next(c for c in result.checks if c.name == "connection_protocol")
        assert protocol_check.passed
        assert "default" in protocol_check.message

    # ------------------------------------------------------------------
    # endpoint URL format checks
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "endpoint",
        [
            "abfss://my-container@mystorageaccount.dfs.core.windows.net/",
            "wasbs://my-container@mystorageaccount.blob.core.windows.net/",
            "https://mystorageaccount.blob.core.windows.net/",
            "http://localhost:10000/devstoreaccount1/",
        ],
    )
    def test_passes_with_valid_endpoint_url(self, endpoint: str) -> None:
        # GIVEN
        databag = {**VALID_DATABAG, "endpoint": endpoint}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        endpoint_check = next(c for c in result.checks if c.name == "endpoint_format")
        assert endpoint_check.passed

    def test_fails_with_invalid_endpoint_url(self) -> None:
        # GIVEN an endpoint with an unrecognised scheme
        databag = {**VALID_DATABAG, "endpoint": "ftp://mystorageaccount.blob.core.windows.net/"}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        endpoint_check = next(c for c in result.checks if c.name == "endpoint_format")
        assert not endpoint_check.passed

    def test_no_endpoint_check_when_endpoint_absent(self) -> None:
        # GIVEN no endpoint field
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "endpoint"}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        endpoint_check_names = [c.name for c in result.checks]
        assert "endpoint_format" not in endpoint_check_names

    def test_result_metadata(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-azure-storage")

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-azure-storage"
        assert result.interface == "azure_storage"
        assert result.level == "simple"
