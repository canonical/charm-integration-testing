# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from .stubs import (
    ApplicationStub,
    CharmBaseStub,
    CharmMetaStub,
    ModelStub,
    RelationMetaStub,
    RelationRoleStub,
    RelationStub,
    UnitStub,
)


def make_charm_from_relation(
    relation: RelationStub,
    role: RelationRoleStub = RelationRoleStub.requires,
    interface_name: str | None = None,
    integrations_count: int = 1,
    local_app_name: str = "app",
    local_model_name: str = "test-model",
    local_model_uuid: str = "11111111-1111-1111-1111-111111111111",
    local_unit_name: str = "app/0",
) -> CharmBaseStub:
    if integrations_count == 1:
        relations_list = [relation]
    else:
        relations_list = [
            RelationStub(relation.name, idx, relation.app, {relation.app: dict(relation.data.get(relation.app, {}))})
            for idx in range(integrations_count)
        ]

    model = ModelStub(
        relations={relation.name: relations_list},
        name=local_model_name,
        uuid=local_model_uuid,
        unit=UnitStub(local_unit_name),
    )
    return CharmBaseStub(
        meta=CharmMetaStub(
            relations={
                relation.name: RelationMetaStub(
                    relation_name=relation.name,
                    role=role,
                    interface_name=interface_name,
                )
            }
        ),
        model=model,
        app=ApplicationStub(name=local_app_name),
        unit=UnitStub(local_unit_name),
    )


def make_charm_from_relation_and_secrets(
    relation: RelationStub, secrets: dict[str, dict[str, str]], role: RelationRoleStub = RelationRoleStub.requires
) -> CharmBaseStub:
    charm = make_charm_from_relation(relation, role)
    charm.model._secrets = secrets
    return charm
