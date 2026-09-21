# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import cast

import ops
import pytest
from pydantic import ValidationError

from validators.base import (
    BasePersistenceValidator,
    BaseValidator,
    PersistenceState,
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


class ConcretePersistenceValidator(BasePersistenceValidator):
    """Minimal in-memory concrete implementation for testing BasePersistenceValidator."""

    def prepare(self) -> PersistenceState:
        return PersistenceState(id=1, ref=1, token="test-token")

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        check = ValidationCheck(name="row_count", passed=True, message="OK")
        result = self._make_result(status="PASS", level="deep", interface="test-interface", checks=[check])
        return result, PersistenceState(id=expected.id, ref=expected.ref + 1, token=expected.token)

    def cleanup(self) -> None:
        pass


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

    def test_resolve_secret_uses_explicit_data_instead_of_databag(self) -> None:
        # GIVEN a relation whose remote databag has no secret, but an explicit
        # (e.g. local app) databag is passed in instead
        app = ApplicationStub()
        relation = RelationStub(name="my-db", id=1, app=app, data={app: {}})
        secrets = {"secret:db-creds": {"username": "secret-user", "password": "pw"}}
        charm = make_charm_from_relation_and_secrets(relation, secrets)
        validator = ConcreteValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        resolved = validator.resolve_secret(
            "secret-uri", "username", "password", data={"secret-uri": "secret:db-creds"}
        )

        # THEN the explicit data was used instead of self.databag
        assert resolved == {"username": "secret-user", "password": "pw"}
        assert charm.model.requested_ids == ["secret:db-creds"]

    def test_validate_schema_uses_explicit_data_instead_of_databag(self) -> None:
        # GIVEN a relation whose remote databag is empty, but an explicit
        # databag with the required fields is passed in instead
        app = ApplicationStub()
        relation = RelationStub(name="my-db", id=1, app=app, data={app: {}})
        charm = make_charm_from_relation(relation)
        validator = ConcreteValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        check = validator.validate_schema(["host", "port"], data={"host": "10.0.0.10", "port": "5432"})

        # THEN the explicit data was used instead of self.databag
        assert check == ValidationCheck(name="schema", passed=True, message="OK")


class TestPersistenceState:
    def test_ref_defaults_to_zero(self) -> None:
        # GIVEN / WHEN
        state = PersistenceState(id=7, token="abc123")

        # THEN
        assert state.ref == 0

    def test_token_is_required(self) -> None:
        # GIVEN / WHEN / THEN a state that could not have come from prepare() is rejected
        with pytest.raises(ValidationError):
            PersistenceState(id=7)

    def test_token_must_be_non_empty(self) -> None:
        # GIVEN / WHEN / THEN an empty token would match records carrying no token at all
        with pytest.raises(ValidationError):
            PersistenceState(id=7, token="")

    def test_serialises_to_json(self) -> None:
        # GIVEN
        state = PersistenceState(id=4, ref=2, token="abc123")

        # WHEN
        json_str = state.model_dump_json()

        # THEN
        assert json_str == '{"id":4,"ref":2,"token":"abc123"}'


class TestBasePersistenceValidator:
    def test_cannot_instantiate_abstract_class(self) -> None:
        # GIVEN / WHEN / THEN
        with pytest.raises(TypeError):
            relation = RelationStub(name="x", id=0)
            charm = make_charm_from_relation(relation)
            BasePersistenceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))  # type: ignore[abstract]

    def test_prepare_returns_persistence_state(self) -> None:
        # GIVEN
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation)
        validator = ConcretePersistenceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        state = validator.prepare()

        # THEN
        assert isinstance(state, PersistenceState)
        assert state.ref == 1

    def test_checkpoint_returns_result_and_advanced_state(self) -> None:
        # GIVEN
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation)
        validator = ConcretePersistenceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))
        expected = PersistenceState(id=1, ref=3, token="test-token")

        # WHEN
        result, new_state = validator.checkpoint(expected)

        # THEN
        assert isinstance(result, ValidationResult)
        assert result.status == "PASS"
        assert new_state.id == expected.id
        assert new_state.ref == expected.ref + 1

    def test_cleanup_does_not_raise(self) -> None:
        # GIVEN
        relation = RelationStub(name="db", id=0)
        charm = make_charm_from_relation(relation)
        validator = ConcretePersistenceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN / THEN
        validator.cleanup()

    def test_shares_relation_helpers_with_base_validator(self) -> None:
        # GIVEN a persistence validator on a relation with a databag
        app = ApplicationStub()
        databag = {"username": "admin"}
        relation = RelationStub(name="my-db", id=3, app=app, data={app: databag})
        charm = make_charm_from_relation(relation, role=RelationRoleStub.requires, interface_name="my-interface")
        validator = ConcretePersistenceValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # THEN the same relation-inspection helpers as BaseValidator are available
        assert validator.endpoint == "my-db"
        assert validator.relation_id == 3
        assert validator.interface == "my-interface"
        assert validator.role == "requires"
        assert validator.databag == databag
        assert validator.relation_exists() is True
