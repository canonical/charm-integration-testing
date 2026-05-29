# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from .stubs import CharmBaseStub, CharmMetaStub, ModelStub, RelationMetaStub, RelationRoleStub, RelationStub


def make_charm_from_relation(
    relation: RelationStub,
    role: RelationRoleStub = RelationRoleStub.requires,
    interface_name: str | None = None,
    integrations_count: int = 1,
) -> CharmBaseStub:
    if integrations_count == 1:
        relations_list = [relation]
    else:
        relations_list = [
            RelationStub(relation.name, idx, relation.app, relation.data) for idx in range(integrations_count)
        ]

    model = ModelStub(relations={relation.name: relations_list})
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
    )


def make_charm_from_relation_and_secrets(
    relation: RelationStub, secrets: dict[str, dict[str, str]], role: RelationRoleStub = RelationRoleStub.requires
) -> CharmBaseStub:
    charm = make_charm_from_relation(relation, role)
    charm.model._secrets = secrets
    return charm
