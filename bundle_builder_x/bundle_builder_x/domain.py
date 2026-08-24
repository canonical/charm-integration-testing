# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import z3  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from .charm import Charm, CharmChannel, CharmConfigValue, CharmResourceValue, EndpointScope, EndpointType
from .juju_version import JujuVersion


class ModelRef(BaseModel):
    """A lightweight reference to a Juju model, carrying both its plain name and optional controller.

    Used internally in the domain layer to avoid embedding ``controller/name`` strings in single fields.
    """

    model_config = ConfigDict(frozen=True)

    name: str | None = None
    controller: str | None = None

    @property
    def key(self) -> str:
        """Domain lookup key: ``controller/name`` when a controller is set, otherwise plain ``name``."""
        if self.controller is not None:
            return f"{self.controller}/{self.name}"
        return self.name or ""


class DomainApplicationEndpoint(BaseModel):
    """Represents an application and one of its endpoints within the solver domain.

    For cross-model integrations, ``model`` identifies the remote model.
    ``ModelRef()`` (the default, with no name set) means "this model" (the model that owns the constraint).
    """

    model_config = ConfigDict(frozen=True)

    application: str
    endpoint: str
    model: ModelRef = Field(default_factory=ModelRef)


class DomainApplicationIntegration(BaseModel):
    """Represents an integration between two application endpoints.

    Endpoints are unordered since we don't know which is requires/provides until charms are resolved.
    For cross-model integrations, ``offer_name`` carries the offer name and ``url`` carries the
    user-supplied offer URL when one was explicitly provided in the spec.  When ``url`` is None,
    extract.py synthesizes the correct URL based on the resolved endpoint role.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    endpoint_1: DomainApplicationEndpoint
    endpoint_2: DomainApplicationEndpoint
    offer_name: str | None = None
    url: str | None = None
    # charm_integration_idx -> Z3 Bool mapping variable.
    # Populated by add_charm_to_domain as charm integrations are discovered.
    charm_integration_ids: dict[int, z3.BoolRef] = Field(default_factory=dict)


class DomainApplication(BaseModel):
    """Per-application state within a model."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    charm: str
    channel: CharmChannel | None = None
    revision: int | None = None
    base: str | None = None
    # charm_id -> Z3 Bool mapping variable
    charm_ids: dict[int, z3.BoolRef] = Field(default_factory=dict)
    # Dedup tracking: charm IDs already added for this application
    charms_added: list[int] = Field(default_factory=list)


class DomainModel(BaseModel):
    """Per-model metadata and user constraints within a global domain."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    arch: str
    platform: str
    juju_version: JujuVersion
    admin: str = "admin"
    ref: ModelRef = Field(default_factory=ModelRef)
    applications: dict[str, DomainApplication] = Field(default_factory=dict)
    application_integrations: list[DomainApplicationIntegration] = Field(default_factory=list)


class DomainCharmEndpoint(BaseModel):
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


class DomainCharmResource(BaseModel):
    """Holds Z3 state for a single resource key on a charm instance."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Z3 String variable for the resource value.  Present only when the key
    # has more than one non-null allowed value, or when None is among the
    # allowed values (optional resource).
    var: z3.ExprRef | None = None
    # Z3 Bool that is True when the resource is set.  Present only when var
    # is set and None is among the allowed values (optional resource).
    isset_var: z3.BoolRef | None = None
    # Fixed value when the override declared exactly one non-null value with
    # no null option.
    default: CharmResourceValue = None
    # True when the override declared exactly one non-null value with no null
    # option: the value is always emitted and no Z3 var is needed.
    fixed_value: bool = False


class DomainCharm(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    exists: z3.BoolRef
    num_units: z3.ArithRef
    spec: Charm
    model: ModelRef
    endpoints: dict[str, DomainCharmEndpoint]
    config: dict[str, DomainCharmConfig] = Field(default_factory=dict)
    resources: dict[str, DomainCharmResource] = Field(default_factory=dict)
    # Dedup tracking: charm IDs added as dependencies of this charm
    charms_added: list[int] = Field(default_factory=list)


class DomainCharmIntegration(BaseModel):
    """A solver-level integration between two charm endpoints.

    Unifies local (same-model) integrations and cross-model integrations into
    a single structure.

    The solver decides whether ``exists`` is true.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    exists: z3.BoolRef

    requires_charm_id: int
    requires_endpoint: str
    provides_charm_id: int
    provides_endpoint: str


class Domain(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Per-model constraints and mappings
    models: dict[ModelRef, DomainModel] = Field(default_factory=dict)

    # Global charm pool (charm_id = list index)
    charms: list[DomainCharm] = Field(default_factory=list)

    # All integrations (local and cross-model); integration_id = list index
    charm_integrations: list[DomainCharmIntegration] = Field(default_factory=list)

    def is_cross_model(self, integration: DomainCharmIntegration) -> bool:
        return self.charms[integration.requires_charm_id].model != self.charms[integration.provides_charm_id].model

    def integration_interface(self, integration: DomainCharmIntegration) -> str:
        return self.charms[integration.requires_charm_id].spec.endpoints[integration.requires_endpoint].interface

    def integration_offer_name(self, integration: DomainCharmIntegration) -> str:
        prov_charm = self.charms[integration.provides_charm_id]
        interface = self.integration_interface(integration)
        return f"{prov_charm.spec.name}-{integration.provides_endpoint}-{interface}-offer".replace("_", "-")


def _refresh_application_integration_mappings(domain: Domain) -> None:
    """Update application-integration mappings to include any newly created charm integrations.

    Re-scans all models each call because a newly created charm integration may complete
    a mapping in any model (as either the local or the remote application). Already-linked
    integration ids are skipped, so repeated calls are safe, though runtime still scales
    with the number of models, application integrations, and charm integrations in the
    domain.
    """
    for mc_model_ref, mc in domain.models.items():
        for app_integration in mc.application_integrations:
            is_cmr = app_integration.endpoint_1.model != app_integration.endpoint_2.model

            for i_idx, integration in enumerate(domain.charm_integrations):
                if i_idx in app_integration.charm_integration_ids:
                    continue  # already linked

                if domain.is_cross_model(integration) != is_cmr:
                    continue

                if not is_cmr:
                    # Local integration: both charms must be in this model.
                    if (
                        domain.charms[integration.requires_charm_id].model != mc_model_ref
                        or domain.charms[integration.provides_charm_id].model != mc_model_ref
                    ):
                        continue

                    orderings = [
                        (
                            app_integration.endpoint_1.application,
                            app_integration.endpoint_2.application,
                            app_integration.endpoint_1.endpoint == integration.requires_endpoint
                            and app_integration.endpoint_2.endpoint == integration.provides_endpoint,
                        ),
                        (
                            app_integration.endpoint_2.application,
                            app_integration.endpoint_1.application,
                            app_integration.endpoint_2.endpoint == integration.requires_endpoint
                            and app_integration.endpoint_1.endpoint == integration.provides_endpoint,
                        ),
                    ]

                    for req_app, prov_app, matches in orderings:
                        if not matches:
                            continue
                        req_has = (
                            req_app in mc.applications
                            and integration.requires_charm_id in mc.applications[req_app].charm_ids
                        )
                        prov_has = (
                            prov_app in mc.applications
                            and integration.provides_charm_id in mc.applications[prov_app].charm_ids
                        )
                        if req_has and prov_has:
                            app_integration.charm_integration_ids[i_idx] = z3.Bool(
                                f"app_integration"
                                f"_{app_integration.endpoint_1.application}:{app_integration.endpoint_1.endpoint}"
                                f"__{app_integration.endpoint_2.application}:{app_integration.endpoint_2.endpoint}"
                                f"_maps_to_charm_integration"
                                f"_{integration.requires_charm_id}:{integration.requires_endpoint}"
                                f"__{integration.provides_charm_id}:{integration.provides_endpoint}"
                            )
                else:
                    # CMR integration: identify local and remote endpoints.
                    local_ep = (
                        app_integration.endpoint_1
                        if app_integration.endpoint_1.model == ModelRef()
                        else app_integration.endpoint_2
                    )
                    remote_ep = (
                        app_integration.endpoint_2
                        if app_integration.endpoint_1.model == ModelRef()
                        else app_integration.endpoint_1
                    )
                    remote_mc = domain.models.get(remote_ep.model)
                    if remote_mc is None:
                        continue  # external CMR - no DomainCharmIntegration exists

                    # Match integration to this CMR by model names and endpoint names.
                    req_model_ref = domain.charms[integration.requires_charm_id].model
                    prov_model_ref = domain.charms[integration.provides_charm_id].model
                    if (
                        req_model_ref == mc_model_ref
                        and integration.requires_endpoint == local_ep.endpoint
                        and prov_model_ref == remote_ep.model
                        and integration.provides_endpoint == remote_ep.endpoint
                    ):
                        local_charm_id = integration.requires_charm_id
                        remote_charm_id = integration.provides_charm_id
                    elif (
                        prov_model_ref == mc_model_ref
                        and integration.provides_endpoint == local_ep.endpoint
                        and req_model_ref == remote_ep.model
                        and integration.requires_endpoint == remote_ep.endpoint
                    ):
                        local_charm_id = integration.provides_charm_id
                        remote_charm_id = integration.requires_charm_id
                    else:
                        continue
                    if (
                        local_ep.application not in mc.applications
                        or local_charm_id not in mc.applications[local_ep.application].charm_ids
                    ):
                        continue
                    if (
                        remote_ep.application not in remote_mc.applications
                        or remote_charm_id not in remote_mc.applications[remote_ep.application].charm_ids
                    ):
                        continue
                    app_integration.charm_integration_ids[i_idx] = z3.Bool(
                        f"app_integration"
                        f"_{local_ep.application}:{local_ep.endpoint}"
                        f"__cmr__{remote_ep.model.key}_{remote_ep.application}:{remote_ep.endpoint}"
                        f"__integration_{i_idx}"
                    )


def add_charm_to_domain(charm: Charm, domain: Domain, model_ref: ModelRef | None = None) -> int:
    # Resolve model_ref: default to the single model if not provided
    if model_ref is None:
        if len(domain.models) == 1:
            model_ref = next(iter(domain.models))
        else:
            raise ValueError("model_ref is required when the domain contains multiple models")

    model = domain.models[model_ref]
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

    # Build per-key resource entries from the override 'resources' list.
    charm_resources: dict[str, DomainCharmResource] = {}
    for key, res_allowed in charm.resources.items():
        res_non_none = [v for v in res_allowed if v is not None]
        if not res_non_none:
            continue
        if len(res_non_none) == 1 and None not in res_allowed:
            charm_resources[key] = DomainCharmResource(
                fixed_value=True,
                default=res_non_none[0],
            )
            continue
        prefix = f"charm_{charm.name}_{charm_id}_resource_{key}"
        res_var: z3.ExprRef = z3.String(prefix)
        res_isset_var = z3.Bool(f"{prefix}_is_set") if None in res_allowed else None
        charm_resources[key] = DomainCharmResource(
            var=res_var,
            isset_var=res_isset_var,
        )

    domain.charms.append(
        DomainCharm(
            exists=z3.Bool(f"charm_{charm.name}_{charm_id}_exists"),
            num_units=z3.Int(f"charm_{charm.name}_{charm_id}_num_units"),
            spec=charm,
            model=model_ref,
            config=charm_config,
            resources=charm_resources,
            endpoints={
                name: DomainCharmEndpoint(
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

    # Note: no integration vars against other domain charms are created here — eager pairing
    # is O(domain size) per add and was the dominant driver of CEGIS blowup. Callers pair with
    # the relevant charm(s) via pair_charms_in_domain(); other connections are discovered
    # lazily by the CEGIS loop (_connect_existing_for_endpoint) on a later iteration.

    # Create application-to-charm mappings for this model's constraints
    for application, domain_app in model.applications.items():
        if (
            domain_app.charm != charm.name
            or (domain_app.channel is not None and domain_app.channel != charm.channel)
            or (domain_app.revision is not None and domain_app.revision != charm.revision)
            or (domain_app.base is not None and domain_app.base != charm.ubuntu_version)
        ):
            continue
        domain_app.charm_ids[charm_id] = z3.Bool(f"app_{application}_maps_to_charm_{charm.name}_{charm_id}")

    # Create integration-to-charm-integration mappings (unified local + CMR).
    _refresh_application_integration_mappings(domain)

    return charm_id


def pair_charms_in_domain(domain: Domain, charm_a_id: int, charm_b_id: int) -> bool:
    """Create integration variables for compatible endpoint pairs between two specific charms.

    Safe to call multiple times for the same pair; already-existing integration variables
    are skipped.  Runs the application-integration mapping scan when new vars are created.

    Returns True if at least one new integration variable was created.
    """
    if charm_a_id == charm_b_id:
        return False  # a charm never relates to itself
    charm_a = domain.charms[charm_a_id]
    charm_b = domain.charms[charm_b_id]
    model_a = charm_a.model
    model_b = charm_b.model
    same_model = model_a == model_b

    created_any = False
    existing_keys = {
        (i.requires_charm_id, i.requires_endpoint, i.provides_charm_id, i.provides_endpoint)
        for i in domain.charm_integrations
    }
    for ep_name_a, ep_a in charm_a.spec.endpoints.items():
        for ep_name_b, ep_b in charm_b.spec.endpoints.items():
            if ep_a.interface != ep_b.interface:
                continue

            if ep_a.type == EndpointType.REQUIRES and ep_b.type == EndpointType.PROVIDES:
                req_cid, req_ep = charm_a_id, ep_name_a
                prov_cid, prov_ep = charm_b_id, ep_name_b
                req_model, prov_model = model_a, model_b
            elif ep_a.type == EndpointType.PROVIDES and ep_b.type == EndpointType.REQUIRES:
                req_cid, req_ep = charm_b_id, ep_name_b
                prov_cid, prov_ep = charm_a_id, ep_name_a
                req_model, prov_model = model_b, model_a
            else:
                continue

            if ep_a.scope == EndpointScope.CONTAINER or ep_b.scope == EndpointScope.CONTAINER:
                if not same_model:
                    continue
                req_platform = domain.models[req_model].platform if req_model in domain.models else None
                prov_platform = domain.models[prov_model].platform if prov_model in domain.models else None
                if req_platform != "machine" or prov_platform != "machine":
                    continue

            # Skip if this integration variable already exists.
            key = (req_cid, req_ep, prov_cid, prov_ep)
            if key in existing_keys:
                continue
            existing_keys.add(key)

            if same_model:
                exists_var = z3.Bool(f"charm_integration_{prov_cid}:{prov_ep}__{req_cid}:{req_ep}_exists")
            else:
                exists_var = z3.Bool(
                    f"cmr__{prov_model.key}__{prov_cid}:{prov_ep}" f"__{req_model.key}__{req_cid}:{req_ep}__exists"
                )

            domain.charm_integrations.append(
                DomainCharmIntegration(
                    exists=exists_var,
                    requires_charm_id=req_cid,
                    requires_endpoint=req_ep,
                    provides_charm_id=prov_cid,
                    provides_endpoint=prov_ep,
                )
            )
            created_any = True

    if not created_any:
        return False

    # Update application-integration mappings to include any newly created vars.
    _refresh_application_integration_mappings(domain)

    return True
