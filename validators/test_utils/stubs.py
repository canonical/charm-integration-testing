from dataclasses import dataclass
from validators.base import ValidationRole
from dataclasses import dataclass, field


@dataclass
class RelationRoleStub:
    """Stub for relation role, commonly used across validator tests."""
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
