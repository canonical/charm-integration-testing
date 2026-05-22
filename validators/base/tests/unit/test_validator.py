# Copyright (C) 2026 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from dataclasses import dataclass, field

import pytest

from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationRole, ValidationResult


@dataclass
class RelationRoleStub:
    name: ValidationRole

@dataclass
class RelationStub:
    name: str
    id: int
    app: object | None = None
    data: dict[object, dict[str, str]] = field(default_factory=dict)


@dataclass
class RelationMetaStub:
    interface_name: str | None
    role: RelationRoleStub


@dataclass
class SecretStub:
    content: dict[str, str]

    def get_content(self) -> dict[str, str]:
        return self.content


@dataclass
class ModelStub:
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    requested_ids: list[str] = field(default_factory=list)

    def get_secret(self, id: str) -> SecretStub:  # noqa: A002
        self.requested_ids.append(id)
        return SecretStub(content=self.secrets[id])


@dataclass
class CharmStub:
    relation_name: str
    interface_name: str = "test-interface"
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.model = ModelStub(secrets=self.secrets)
        self.meta = type(
            "MetaStub",
            (),
            {"relations": {self.relation_name: RelationMetaStub(interface_name=self.interface_name, role=RelationRoleStub("requires"))}},
        )()


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
        validator = ConcreteValidator(CharmStub(relation_name="my-db"), RelationStub(name="my-db", id=0))  # type: ignore[arg-type]
        charm = validator.charm
        charm.meta = type(
            "MetaStub",
            (),
            {"relations": {**charm.meta.relations, charm.relation_name: RelationMetaStub(interface_name=charm.interface_name, role=RelationRoleStub("provides"))}},
        )()

        # WHEN
        role = validator.role

        # THEN
        assert role == "provides"

    def test_validate_returns_result(self) -> None:
        # GIVEN
        validator = ConcreteValidator(CharmStub(relation_name="my-db"), RelationStub(name="my-db", id=0))  # type: ignore[arg-type]

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert isinstance(result, ValidationResult)
        assert result.status == "PASS"
        assert result.level == "simple"

    def test_cannot_instantiate_abstract_class(self) -> None:
        # GIVEN / WHEN / THEN
        with pytest.raises(TypeError):
            BaseValidator(object(), RelationStub(name="x", id=0))  # type: ignore[abstract, arg-type]

    def test_databag_is_empty_when_relation_has_no_app(self) -> None:
        # GIVEN
        relation = RelationStub(name="my-db", id=1, app=None, data={})
        validator = ConcreteValidator(CharmStub(relation_name="my-db"), relation)  # type: ignore[arg-type]

        # WHEN / THEN
        assert validator.databag == {}

    def test_databag_reads_relation_app_databag(self) -> None:
        # GIVEN
        app = object()
        relation_data = {"username": "admin", "password": "secret"}
        relation = RelationStub(name="my-db", id=1, app=app, data={app: relation_data})
        validator = ConcreteValidator(CharmStub(relation_name="my-db"), relation)  # type: ignore[arg-type]

        # WHEN
        databag = validator.databag
        databag["username"] = "changed"

        # THEN
        assert validator.databag == relation_data
        assert relation_data["username"] == "admin"

    @pytest.mark.parametrize(
        "app,exists",
        [
            (None, False),
            (object(), True),
        ],
    )
    def test_relation_exists_reflects_presence_of_relation_app(self, app: object | None, exists: bool) -> None:
        # GIVEN
        relation = RelationStub(name="my-db", id=1, app=app, data={})
        validator = ConcreteValidator(CharmStub(relation_name="my-db"), relation)  # type: ignore[arg-type]

        # WHEN / THEN
        assert validator.relation_exists() is exists

    def test_resolve_secret_reads_secret_when_uri_is_present(self) -> None:
        # GIVEN
        app = object()
        relation = RelationStub(
            name="my-db",
            id=1,
            app=app,
            data={app: {"secret-uri": "secret:db-creds", "username": "plain-user"}},
        )
        charm = CharmStub(
            relation_name="my-db", secrets={"secret:db-creds": {"username": "secret-user", "password": "pw"}}
        )
        validator = ConcreteValidator(charm, relation)  # type: ignore[arg-type]

        # WHEN
        resolved = validator.resolve_secret("secret-uri", "username", "password")

        # THEN
        assert resolved == {"username": "secret-user", "password": "pw"}
        assert charm.model.requested_ids == ["secret:db-creds"]

    def test_resolve_secret_falls_back_to_plaintext_fields_without_uri(self) -> None:
        # GIVEN
        app = object()
        relation = RelationStub(
            name="my-db",
            id=1,
            app=app,
            data={app: {"username": "plain-user", "password": "plain-pw", "extra": "x"}},
        )
        charm = CharmStub(relation_name="my-db")
        validator = ConcreteValidator(charm, relation)  # type: ignore[arg-type]

        # WHEN
        resolved = validator.resolve_secret("secret-uri", "username", "password", "missing")

        # THEN
        assert resolved == {"username": "plain-user", "password": "plain-pw"}
        assert charm.model.requested_ids == []

    def test_validate_schema_reports_missing_required_fields(self) -> None:
        # GIVEN
        app = object()
        relation = RelationStub(
            name="my-db",
            id=1,
            app=app,
            data={app: {"host": "10.0.0.10", "port": ""}},
        )
        validator = ConcreteValidator(CharmStub(relation_name="my-db"), relation)  # type: ignore[arg-type]

        # WHEN
        check = validator.validate_schema(["host", "port", "user"])

        # THEN
        assert check.passed is False
        assert check.message == "Missing: port, user"

    def test_validate_schema_merges_resolved_credentials(self) -> None:
        # GIVEN
        app = object()
        relation = RelationStub(name="my-db", id=1, app=app, data={app: {"host": "10.0.0.10"}})
        validator = ConcreteValidator(CharmStub(relation_name="my-db"), relation)  # type: ignore[arg-type]

        # WHEN
        check = validator.validate_schema(
            ["host", "username", "password"],
            creds={"username": "secret-user", "password": "secret-pw"},
        )

        # THEN
        assert check == ValidationCheck(name="schema", passed=True, message="OK")
