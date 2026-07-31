# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import cast

import ops
import pytest

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)
from validators.test_utils.helpers import (
    make_charm_from_relation,
    make_charm_from_relation_and_secrets,
)
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)


class ConcreteValidator(BaseValidator):
    """Minimal concrete implementation for testing BaseValidator."""

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        return self._make_result(status="PASS", interface="test-interface", level=level)


class TestValidationCheck:
    def test_message_defaults_to_empty_string(self) -> None:
        # GIVEN / WHEN
        check = ValidationCheck(name="schema", passed=False)

        # THEN
        assert check.message == ""


class TestValidationResult:
    def test_serialises_to_json(self) -> None:
        # GIVEN
        result = ValidationResult(
            status="PASS",
            endpoint="db",
            interface="postgresql_client",
            role="requires",
            level="simple",
            relation_id=1,
        )

        # WHEN
        json_str = result.model_dump_json()

        # THEN
        assert '"status":"PASS"' in json_str
        assert '"interface":"postgresql_client"' in json_str


class TestBaseValidator:
    def test_role_property_returns_set_value(self) -> None:
        # GIVEN
        relation = RelationStub(name="my-db", id=0)
        charm = make_charm_from_relation(relation, RelationRoleStub.provides)
        validator = ConcreteValidator(cast(ops.CharmBase, charm), relation=cast(ops.Relation, relation))

        # WHEN
        role = validator.role

        # THEN
        assert role == "provides"

    def test_validate_returns_result(self) -> None:
        # GIVEN
        relation = RelationStub(name="my-db", id=0)
        charm = make_charm_from_relation(relation)
        validator = ConcreteValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert isinstance(result, ValidationResult)
        assert result.status == "PASS"
        assert result.level == "simple"

    def test_cannot_instantiate_abstract_class(self) -> None:
        # GIVEN / WHEN / THEN
        with pytest.raises(TypeError):
            relation = RelationStub(name="x", id=0)
            charm = make_charm_from_relation(relation)
            BaseValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))  # type: ignore[abstract]

    def test_databag_reads_relation_app_databag(self) -> None:
        # GIVEN
        app = ApplicationStub()
        databag = {"username": "admin", "password": "secret"}
        relation = RelationStub(name="my-db", id=1, app=app, data={app: databag})
        charm = make_charm_from_relation(relation)
        validator = ConcreteValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        validator.databag["username"] = "changed"

        # THEN
        assert validator.databag == databag
        assert databag["username"] == "admin"

    @pytest.mark.parametrize("exists", [False, True])
    def test_relation_exists_reflects_presence_of_relation_app(self, exists: bool) -> None:
        # GIVEN
        app = ApplicationStub()
        relation = RelationStub(name="my-db", id=1, app=app)
        if not exists:
            relation.data = {}
        charm = make_charm_from_relation(relation)
        validator = ConcreteValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN / THEN
        assert validator.relation_exists() is exists

    def test_resolve_secret_reads_secret_when_uri_is_present(self) -> None:
        # GIVEN
        app = ApplicationStub()
        relation = RelationStub(
            name="my-db",
            id=1,
            app=app,
            data={app: {"secret-uri": "secret:db-creds", "username": "plain-user"}},
        )
        secrets = {"secret:db-creds": {"username": "secret-user", "password": "pw"}}
        charm = make_charm_from_relation_and_secrets(relation, secrets)
        validator = ConcreteValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        resolved = validator.resolve_secret("secret-uri", "username", "password")

        # THEN
        assert resolved == {"username": "secret-user", "password": "pw"}
        assert charm.model.requested_ids == ["secret:db-creds"]

    def test_resolve_secret_falls_back_to_plaintext_fields_without_uri(self) -> None:
        # GIVEN
        app = ApplicationStub()
        relation = RelationStub(
            name="my-db",
            id=1,
            app=app,
            data={app: {"username": "plain-user", "password": "plain-pw", "extra": "x"}},
        )
        charm = make_charm_from_relation(relation)
        validator = ConcreteValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        resolved = validator.resolve_secret("secret-uri", "username", "password", "missing")

        # THEN
        assert resolved == {"username": "plain-user", "password": "plain-pw"}
        assert charm.model.requested_ids == []

    def test_validate_schema_reports_missing_required_fields(self) -> None:
        # GIVEN
        app = ApplicationStub()
        relation = RelationStub(
            name="my-db",
            id=1,
            app=app,
            data={app: {"host": "10.0.0.10", "port": ""}},
        )
        charm = make_charm_from_relation(relation)
        validator = ConcreteValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        check = validator.validate_schema(["host", "port", "user"])

        # THEN
        assert check.passed is False
        assert check.message == "Missing: port, user"

    def test_validate_schema_merges_resolved_credentials(self) -> None:
        # GIVEN
        app = ApplicationStub()
        relation = RelationStub(name="my-db", id=1, app=app, data={app: {"host": "10.0.0.10"}})
        charm = make_charm_from_relation(relation)
        validator = ConcreteValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        check = validator.validate_schema(
            ["host", "username", "password"],
            creds={"username": "secret-user", "password": "secret-pw"},
        )

        # THEN
        assert check == ValidationCheck(name="schema", passed=True, message="OK")
