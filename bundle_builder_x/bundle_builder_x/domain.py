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

import z3  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from .charm import Charm, CharmChannel, CharmConfigValue, EndpointType
from .juju_version import JujuVersion


class IntegrationConstraint(BaseModel):
    """User-provided constraint specifying an integration between two application endpoints.

    This is an input constraint, not to be confused with Integration which is the output format.
    """

    model_config = ConfigDict(frozen=True)

    application_1: str
    endpoint_1: str
    application_2: str
    endpoint_2: str


class DomainApplicationEndpoint(BaseModel):
    """Represents an application and one of its endpoints within the solver domain."""

    model_config = ConfigDict(frozen=True)

    application: str
    endpoint: str


class CharmEndpoint(BaseModel):
    """Represents a charm instance and one of its endpoints."""

    model_config = ConfigDict(frozen=True)

    charm_id: int
    endpoint: str


class ApplicationIntegration(BaseModel):
    """Represents an integration between two application endpoints.

    Endpoints are unordered since we don't know which is requires/provides until charms are resolved.
    """

    model_config = ConfigDict(frozen=True)

    endpoint_1: DomainApplicationEndpoint
    endpoint_2: DomainApplicationEndpoint


class CharmIntegration(BaseModel):
    """Represents an integration between two charm endpoints.

    Endpoints are ordered semantically: requires comes before provides.
    """

    model_config = ConfigDict(frozen=True)

    requires_endpoint: CharmEndpoint
    provides_endpoint: CharmEndpoint


class ApplicationToCharmMapping(BaseModel):
    """Represents a mapping from an application to a charm instance."""

    model_config = ConfigDict(frozen=True)

    application: str
    charm_id: int


class ApplicationConstraint(BaseModel):
    model_config = ConfigDict(frozen=True)

    charm: str
    channel: CharmChannel | None = None
    revision: int | None = None
    base: str | None = None


class CrossModelRemote(BaseModel):
    """Remote side of a cross-model integration."""

    model_config = ConfigDict(frozen=True)

    model: str
    application: str
    endpoint: str
    offer_name: str
    url: str | None = None


class CrossModelIntegrationConstraint(BaseModel):
    """User-provided constraint for a cross-model integration.

    The local side is an application+endpoint in the current model.
    The remote side is an application+endpoint in another model.
    """

    model_config = ConfigDict(frozen=True)

    local_application: str
    local_endpoint: str
    remote: CrossModelRemote


class ModelConstraints(BaseModel):
    """Per-model metadata and user constraints within a global domain."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    arch: str
    platform: str
    juju_version: JujuVersion
    controller: str | None = None
    application_constraints: dict[str, ApplicationConstraint] = Field(default_factory=dict)
    integration_constraints: set[ApplicationIntegration] = Field(default_factory=set)
    cross_model_constraints: list[CrossModelIntegrationConstraint] = Field(default_factory=list)

    # Per-model mapping state (Z3 variables scoped to this model's applications)
    application_to_charm: dict[ApplicationToCharmMapping, z3.BoolRef] = Field(default_factory=dict)
    application_integration_to_charm_integration: dict[tuple[ApplicationIntegration, CharmIntegration], z3.BoolRef] = (
        Field(default_factory=dict)
    )

    # Per-model dedup tracking
    charms_added_for_application: dict[str, list[int]] = Field(default_factory=dict)


class PotentialCMR(BaseModel):
    """A solver-discovered cross-model integration between charms in different models.

    Created eagerly when a new charm is added and an interface matches a charm
    in another model. The solver decides whether ``exists`` is true.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    exists: z3.BoolRef

    requires_model: str
    requires_charm_id: int
    requires_endpoint: str

    provides_model: str
    provides_charm_id: int
    provides_endpoint: str

    interface: str
    offer_name: str


class DomainEndpoint(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    count: z3.ArithRef
    integrated: z3.BoolRef
    # One Z3 Bool per feature declared on this endpoint in the charm spec.
    # Each bool is constrained to equal `endpoint.integrated` in add_charm_metadata_constraints.
    features: dict[str, z3.BoolRef] = Field(default_factory=dict)


class DomainCharmConfig(BaseModel):
    """Holds Z3 state for a single config key on a charm instance."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Z3 variable for the config value.  Present only when the key is declared
    # in the override 'configs' list with more than one non-null allowed value,
    # or when None is among the allowed values (optional config).
    var: z3.ExprRef | None = None
    # Z3 Bool that is True when the config is set to a concrete value.  Present
    # only when var is set and None is among the allowed values (optional config).
    isset_var: z3.BoolRef | None = None
    # Default value from the Charmhub API.  None means the charm declares no
    # default for this key (i.e. the Charmhub default is null or absent).
    default: CharmConfigValue = None
    # True when the override declared exactly one non-null value with no null
    # option: the value is always emitted and no Z3 var is needed.
    fixed_value: bool = False


class DomainCharm(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    exists: z3.BoolRef
    spec: Charm
    endpoints: dict[str, DomainEndpoint]
    config: dict[str, DomainCharmConfig] = Field(default_factory=dict)


class DomainCharmIntegration(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    exists: z3.BoolRef


class Domain(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Per-model constraints and mappings
    models: dict[str, ModelConstraints] = Field(default_factory=dict)

    # Global charm pool (charm_id = list index)
    charms: list[DomainCharm] = Field(default_factory=list)
    charm_to_model: dict[int, str] = Field(default_factory=dict)

    # Local (same-model) integrations only
    charm_integrations: dict[CharmIntegration, DomainCharmIntegration] = Field(default_factory=dict)

    # Solver-discovered cross-model integrations
    potential_cmrs: list[PotentialCMR] = Field(default_factory=list)

    # Global dependency tracking to avoid redundant charm additions
    charms_added_for_charm: dict[int, list[int]] = Field(default_factory=dict)


def _build_app_integrations(
    integrations: set[IntegrationConstraint],
    applications: dict[str, ApplicationConstraint],
) -> set[ApplicationIntegration]:
    """Convert user-facing IntegrationConstraints to canonical ApplicationIntegration models."""
    app_integrations: set[ApplicationIntegration] = set()
    for integration in integrations:
        app_1 = integration.application_1
        ep_1 = integration.endpoint_1
        app_2 = integration.application_2
        ep_2 = integration.endpoint_2

        for app in [app_1, app_2]:
            if app not in applications:
                raise ValueError(
                    f"Integration references undefined application '{app}'. "
                    f"Available applications: {', '.join(sorted(applications.keys()))}"
                )

        endpoints = [(app_1, ep_1), (app_2, ep_2)]
        sorted_eps = sorted(endpoints, key=lambda e: (e[0], e[1]))
        app_integrations.add(
            ApplicationIntegration(
                endpoint_1=DomainApplicationEndpoint(application=sorted_eps[0][0], endpoint=sorted_eps[0][1]),
                endpoint_2=DomainApplicationEndpoint(application=sorted_eps[1][0], endpoint=sorted_eps[1][1]),
            )
        )
    return app_integrations


class ModelInit(BaseModel):
    """Input data for initializing a single model within a global domain."""

    model_config = ConfigDict(frozen=True)

    applications: dict[str, ApplicationConstraint]
    platform: str
    arch: str
    juju_version: JujuVersion
    integrations: set[IntegrationConstraint] = Field(default_factory=set)
    cross_model_integrations: list[CrossModelIntegrationConstraint] = Field(default_factory=list)
    controller: str | None = None


def initialize_global_domain(
    models: dict[str, ModelInit],
) -> Domain:
    """Initialize a global domain containing multiple models."""
    domain = Domain()

    for model_name, init in models.items():
        app_integrations = _build_app_integrations(init.integrations, init.applications)

        for cmr in init.cross_model_integrations:
            if cmr.local_application not in init.applications:
                raise ValueError(
                    f"Cross-model integration references undefined local application '{cmr.local_application}'. "
                    f"Available applications: {', '.join(sorted(init.applications.keys()))}"
                )

        domain.models[model_name] = ModelConstraints(
            arch=init.arch,
            platform=init.platform,
            juju_version=init.juju_version,
            controller=init.controller,
            application_constraints=init.applications,
            integration_constraints=app_integrations,
            cross_model_constraints=init.cross_model_integrations,
        )

    return domain


def add_charm_to_domain(charm: Charm, domain: Domain, model_name: str | None = None) -> int:
    # Resolve model_name: default to the single model if not provided
    if model_name is None:
        if len(domain.models) == 1:
            model_name = next(iter(domain.models))
        else:
            raise ValueError("model_name is required when the domain contains multiple models")

    model = domain.models[model_name]
    charm_id = len(domain.charms)

    # Build per-key config entries.  Start from Charmhub defaults (all known keys),
    # then overlay Z3 variables for keys declared in the override 'configs' list.
    charm_config: dict[str, DomainCharmConfig] = {
        key: DomainCharmConfig(default=default) for key, default in charm.config_defaults.items()
    }
    for key, allowed in charm.configs.items():
        non_none = [v for v in allowed if v is not None]
        if not non_none:
            continue
        existing = charm_config.get(key)
        # Single non-null value with no null option: always emit, no Z3 var needed.
        if len(non_none) == 1 and None not in allowed:
            charm_config[key] = DomainCharmConfig(
                fixed_value=True,
                default=non_none[0],
            )
            continue
        prefix = f"charm_{charm.name}_{charm_id}_config_{key}"
        if all(isinstance(v, bool) for v in non_none):
            var: z3.ExprRef = z3.Bool(prefix)
        elif all(isinstance(v, int) and not isinstance(v, bool) for v in non_none):
            var = z3.Int(prefix)
        elif all(isinstance(v, float) for v in non_none):
            var = z3.Real(prefix)
        elif all(isinstance(v, str) for v in non_none):
            var = z3.String(prefix)
        else:
            raise ValueError(
                f"Config key {key!r} for charm {charm.name!r} has mixed-type allowed values: "
                f"{[type(v).__name__ for v in non_none]}"
            )
        isset_var = z3.Bool(f"{prefix}_is_set") if None in allowed else None
        charm_config[key] = DomainCharmConfig(
            var=var,
            isset_var=isset_var,
            default=existing.default if existing is not None else None,
        )

    domain.charms.append(
        DomainCharm(
            exists=z3.Bool(f"charm_{charm.name}_{charm_id}_exists"),
            spec=charm,
            config=charm_config,
            endpoints={
                name: DomainEndpoint(
                    count=z3.Int(f"charm_{charm.name}_{charm_id}_endpoint_{name}_count"),
                    integrated=z3.Bool(f"charm_{charm.name}_{charm_id}_endpoint_{name}_integrated"),
                    features={
                        f: z3.Bool(f"charm_{charm.name}_{charm_id}_endpoint_{name}_feature_{f}")
                        for f in endpoint.features
                    },
                )
                for name, endpoint in charm.endpoints.items()
            },
        )
    )
    domain.charm_to_model[charm_id] = model_name

    # Pair new charm with all existing charms
    for other_charm_id, other_charm in enumerate(domain.charms):
        if other_charm_id == charm_id:
            continue
        other_model_name = domain.charm_to_model[other_charm_id]
        same_model = other_model_name == model_name

        for endpoint_name, endpoint in charm.endpoints.items():
            for other_endpoint_name, other_endpoint in other_charm.spec.endpoints.items():
                if endpoint.interface != other_endpoint.interface:
                    continue

                # Determine semantic ordering
                if endpoint.type == EndpointType.REQUIRES and other_endpoint.type == EndpointType.PROVIDES:
                    req_charm_id, req_ep = charm_id, endpoint_name
                    prov_charm_id, prov_ep = other_charm_id, other_endpoint_name
                    req_model, prov_model = model_name, other_model_name
                elif endpoint.type == EndpointType.PROVIDES and other_endpoint.type == EndpointType.REQUIRES:
                    req_charm_id, req_ep = other_charm_id, other_endpoint_name
                    prov_charm_id, prov_ep = charm_id, endpoint_name
                    req_model, prov_model = other_model_name, model_name
                else:
                    continue

                if same_model:
                    # Local integration within the same model
                    charm_integration = CharmIntegration(
                        requires_endpoint=CharmEndpoint(charm_id=req_charm_id, endpoint=req_ep),
                        provides_endpoint=CharmEndpoint(charm_id=prov_charm_id, endpoint=prov_ep),
                    )
                    domain.charm_integrations[charm_integration] = DomainCharmIntegration(
                        exists=z3.Bool(f"charm_integration_{prov_charm_id}:{prov_ep}__{req_charm_id}:{req_ep}_exists")
                    )
                else:
                    # Cross-model: create a PotentialCMR for the solver to decide
                    prov_charm_name = domain.charms[prov_charm_id].spec.name
                    offer_name = f"{prov_charm_name}-{prov_ep}-{endpoint.interface}-offer"
                    domain.potential_cmrs.append(
                        PotentialCMR(
                            exists=z3.Bool(
                                f"cmr__{prov_model}__{prov_charm_id}:{prov_ep}"
                                f"__{req_model}__{req_charm_id}:{req_ep}__exists"
                            ),
                            requires_model=req_model,
                            requires_charm_id=req_charm_id,
                            requires_endpoint=req_ep,
                            provides_model=prov_model,
                            provides_charm_id=prov_charm_id,
                            provides_endpoint=prov_ep,
                            interface=endpoint.interface,
                            offer_name=offer_name,
                        )
                    )

    # Create application-to-charm mappings for this model's constraints
    for application, constraints in model.application_constraints.items():
        if (
            constraints.charm != charm.name
            or (constraints.channel is not None and constraints.channel != charm.channel)
            or (constraints.revision is not None and constraints.revision != charm.revision)
            or (constraints.base is not None and constraints.base != charm.ubuntu_version)
        ):
            continue
        mapping = ApplicationToCharmMapping(application=application, charm_id=charm_id)
        model.application_to_charm[mapping] = z3.Bool(f"app_{application}_maps_to_charm_{charm.name}_{charm_id}")

    # Create app-integration-to-charm-integration mappings for this model
    for app_integration in model.integration_constraints:
        for charm_integration in domain.charm_integrations:
            charm_req_ep = charm_integration.requires_endpoint
            charm_prov_ep = charm_integration.provides_endpoint

            # Only consider integrations where both charms are in this model
            if (
                domain.charm_to_model.get(charm_req_ep.charm_id) != model_name
                or domain.charm_to_model.get(charm_prov_ep.charm_id) != model_name
            ):
                continue

            orderings = [
                (
                    app_integration.endpoint_1.application,
                    app_integration.endpoint_2.application,
                    app_integration.endpoint_1.endpoint == charm_req_ep.endpoint
                    and app_integration.endpoint_2.endpoint == charm_prov_ep.endpoint,
                ),
                (
                    app_integration.endpoint_2.application,
                    app_integration.endpoint_1.application,
                    app_integration.endpoint_2.endpoint == charm_req_ep.endpoint
                    and app_integration.endpoint_1.endpoint == charm_prov_ep.endpoint,
                ),
            ]

            for req_app, prov_app, matches in orderings:
                if not matches:
                    continue

                req_mapping = ApplicationToCharmMapping(application=req_app, charm_id=charm_req_ep.charm_id)
                prov_mapping = ApplicationToCharmMapping(application=prov_app, charm_id=charm_prov_ep.charm_id)

                if req_mapping in model.application_to_charm and prov_mapping in model.application_to_charm:
                    model.application_integration_to_charm_integration[(app_integration, charm_integration)] = z3.Bool(
                        f"app_integration_{app_integration.endpoint_1.application}:{app_integration.endpoint_1.endpoint}__{app_integration.endpoint_2.application}:{app_integration.endpoint_2.endpoint}_maps_to_charm_integration_{charm_req_ep.charm_id}:{charm_req_ep.endpoint}__{charm_prov_ep.charm_id}:{charm_prov_ep.endpoint}"
                    )

    return charm_id
