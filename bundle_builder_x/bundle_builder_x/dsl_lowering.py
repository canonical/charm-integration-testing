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

"""Z3 lowering pass for the constraint DSL.

Walks a typed AnyExpr AST (produced by constraints_dsl.parse_constraint) and
emits a Z3 BoolRef that can be added to a solver.

Semantics
---------
endpoint[x]
    A RelationSet — an intermediate value resolved as a list of endpoint names
    on the current charm.  Never appears as a top-level Z3 expression.

len(endpoint[x])
    Z3 Int: the count of active integrations on endpoint x.

bool(endpoint[x])
    Z3 Bool: True iff at least one integration exists on endpoint x.

features(endpoint[x])
    Resolved as a dict[str, z3.BoolRef] — one bool per feature declared on
    endpoint x in the spec.  Each bool is True iff the endpoint is integrated
    (enforced separately in add_charm_metadata_constraints).
    "f" not declared in the spec -> False (via .get fallback in lowering).

config[k]
    Z3 ExprRef for config key k.  Sort is inferred from the allowed values when
    the domain charm is built.  Raises DSLLoweringError if k is not declared.

charms(endpoint[x])
    Z3 Set(Int) of peer charm IDs integrated on endpoint x.

reachable(endpoint[x])
    Z3 Set(Int) of peer charm IDs reachable via endpoint x, including proxy
    chains declared in domain charms' specs.  A proxy {requires: R, provides: P}
    on charm C means C passes certificates from its R endpoint to consumers of
    its P endpoint; those consumers are therefore reachable from the cert-trust
    anchor.  Reachability is computed by fixed-point iteration up to
    len(domain.charms) hops.

{self}
    Z3 Set(Int) containing only the current charm's ID.

A >= B  (CharmSet)
    Z3 IsSubset(B, A)  (A is a superset of B).

features(x) == {"f"}
    Z3 And: feature f is active AND no other declared feature is active.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TypeAlias

import z3  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from .assertion_tags import AssertionTag, CharmPayload, PeerChannelMismatchTag
from .charm import CharmConfigValue
from .constraints_dsl import (
    AndExpr,
    AnyExpr,
    ArithExpr,
    BoolFunc,
    ChannelsExpr,
    CharmsExpr,
    CompareExpr,
    ConfigExpr,
    DSLType,
    EndpointExpr,
    FeaturesExpr,
    ImpliesExpr,
    InExpr,
    IntLit,
    IntLiteralSet,
    LenExpr,
    NotExpr,
    OrExpr,
    ReachableExpr,
    RevisionsExpr,
    RisksExpr,
    SelfExpr,
    SetConfigExpr,
    SetOpExpr,
    StrLit,
    StrLiteralSet,
    TracksExpr,
)
from .domain import Domain, DomainCharm

# ---------------------------------------------------------------------------
# Internal lowering value types
# ---------------------------------------------------------------------------

# RelationSet: a list of endpoint names belonging to the current charm.
_EndpointNames: TypeAlias = list[str]

# SET_STR from features(): a per-feature Z3 Bool dict.
_FeatureSet: TypeAlias = dict[str, z3.BoolRef]


@dataclass(frozen=True)
class _ChannelSetEntry:
    """One peer charm's contribution to a channel-related set (tracks, risks, etc.)."""

    charm_id: int
    charm_name: str
    value: str | int
    condition: z3.BoolRef
    endpoint: str | None  # None means this entry came from {self}


@dataclass
class _ChannelSet:
    """A Z3 set expression paired with per-peer metadata for expansion hints."""

    z3_set: z3.ExprRef
    entries: list[_ChannelSetEntry]
    kind: str  # "track", "risk", "channel", or "revision"


# Any value produced by an internal lowering step.
_LoweredValue: TypeAlias = z3.ExprRef | _EndpointNames | _FeatureSet | _ChannelSet


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DSLLoweringError(ValueError):
    """Raised when a DSL expression cannot be lowered to Z3 for a specific charm."""


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubAssertion:
    """An auxiliary tracked assertion emitted alongside the main Z3 expression.

    These carry structured tags for the unsat core so the bundle builder knows
    how to expand the domain (e.g. fetch a peer charm on a different track).
    """

    expr: z3.BoolRef
    tag: AssertionTag


@dataclass
class LoweringResult:
    """The output of lower(): a Z3 boolean expression plus expansion hints."""

    expr: z3.BoolRef
    sub_assertions: list[SubAssertion] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public context and entry point
# ---------------------------------------------------------------------------


class LoweringContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    charm_id: int
    domain_charm: DomainCharm
    domain: Domain
    sub_assertions: list[SubAssertion] = Field(default_factory=list)


def lower(expr: AnyExpr, ctx: LoweringContext) -> LoweringResult:
    """Lower a boolean DSL constraint AST to a Z3 BoolRef.

    Returns a LoweringResult containing the Z3 expression and any
    sub-assertions that provide expansion hints for the unsat core.

    The caller is responsible for guarding the result with
    ``z3.Implies(charm.exists, result.expr)`` before adding it to the solver.

    Raises:
        DSLLoweringError: if a referenced endpoint, config key, or feature
            cannot be resolved against the context charm.
    """
    ctx.sub_assertions = []
    result = _lower(expr, ctx)
    if not isinstance(result, z3.ExprRef):
        raise DSLLoweringError(
            f"Top-level constraint must produce a Bool Z3 expression, " f"got internal type {type(result).__name__}"
        )
    return LoweringResult(expr=result, sub_assertions=list(ctx.sub_assertions))


def config_value_to_z3(var: z3.ExprRef, value: CharmConfigValue) -> z3.BoolRef:
    """Return a Z3 equality constraint ``var == z3_literal(value)``.

    Exported so that constraints.py can use the same conversion for config
    domain constraints without duplicating the type-dispatch logic.
    """
    if value is None:
        raise DSLLoweringError("None is not a concrete config value")
    if isinstance(value, bool):
        return var == z3.BoolVal(value)
    if isinstance(value, int):
        return var == z3.IntVal(value)
    if isinstance(value, float):
        return var == z3.RealVal(value)
    if isinstance(value, str):
        return var == z3.StringVal(value)
    raise DSLLoweringError(f"Unsupported config value type: {type(value).__name__}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _lower_as_z3(expr: AnyExpr, ctx: LoweringContext) -> z3.ExprRef:
    """Lower expr and assert the result is a Z3 expression."""
    result = _lower(expr, ctx)
    if isinstance(result, _ChannelSet):
        return result.z3_set
    if not isinstance(result, z3.ExprRef):
        raise DSLLoweringError(f"Expected Z3 expression, got {type(result).__name__} for {type(expr).__name__}")
    return result


def _lower_as_endpoints(expr: AnyExpr, ctx: LoweringContext) -> _EndpointNames:
    """Lower expr and assert the result is a list of endpoint names."""
    result = _lower(expr, ctx)
    if not isinstance(result, list):
        raise DSLLoweringError(
            f"Expected endpoint name list (RelationSet), got {type(result).__name__} for {type(expr).__name__}"
        )
    return result


def _lower_as_features(expr: AnyExpr, ctx: LoweringContext) -> _FeatureSet:
    """Lower expr and assert the result is a feature dict."""
    result = _lower(expr, ctx)
    if not isinstance(result, dict):
        raise DSLLoweringError(
            f"Expected feature dict (SET_STR), got {type(result).__name__} for {type(expr).__name__}"
        )
    return result


def _charm_set_for_endpoints(charm_id: int, endpoint_names: _EndpointNames, domain: Domain) -> z3.ExprRef:
    """Build a Z3 Set(Int) of peer charm IDs integrated on the given endpoints."""
    result: z3.ExprRef = z3.EmptySet(z3.IntSort())
    for integration in domain.charm_integrations:
        if integration.requires_charm_id == charm_id and integration.requires_endpoint in endpoint_names:
            peer_id = integration.provides_charm_id
        elif integration.provides_charm_id == charm_id and integration.provides_endpoint in endpoint_names:
            peer_id = integration.requires_charm_id
        else:
            continue
        result = z3.If(integration.exists, z3.SetAdd(result, z3.IntVal(peer_id)), result)

    return result


def _reachable_set(charm_id: int, endpoint_name: str, spec: object, domain: Domain) -> z3.ExprRef:
    """Build a Z3 Set(Int) of charm IDs reachable from the given endpoint.

    Reachability follows proxy chains declared in spec.proxies of each domain charm.

    A proxy ``{interface, requires: R, provides: P}`` on charm C means: C passes
    certificates received on its ``R`` endpoint down to consumers of its ``P``
    endpoint.  So if C's ``R`` endpoint connects to a charm that is already
    reachable (i.e. its certificate is trusted), then C itself is reachable.

    The fixed-point iteration adds at most one new charm per step, so it
    converges in at most ``len(domain.charms)`` iterations.
    """
    # Seed: charms directly connected to this endpoint
    result: z3.ExprRef = _charm_set_for_endpoints(charm_id, [endpoint_name], domain)

    # Iterate to fixed point
    for _ in range(len(domain.charms)):
        extended = result
        for peer_charm_id, peer_charm in enumerate(domain.charms):
            for proxy in peer_charm.spec.proxies:
                # Rule: if peer_charm's 'requires' endpoint (proxy.requires) connects
                # to a charm already in result, peer_charm itself is reachable.
                peer_in_chain: z3.ExprRef = z3.BoolVal(False)
                for integration in domain.charm_integrations:
                    if domain.is_cross_model(integration):
                        continue  # proxy chains are local-only
                    if (
                        integration.requires_charm_id == peer_charm_id
                        and integration.requires_endpoint == proxy.requires
                    ):
                        other_id = integration.provides_charm_id
                    elif (
                        integration.provides_charm_id == peer_charm_id
                        and integration.provides_endpoint == proxy.requires
                    ):
                        other_id = integration.requires_charm_id
                    else:
                        continue
                    peer_in_chain = z3.Or(
                        peer_in_chain,
                        z3.And(integration.exists, z3.IsMember(z3.IntVal(other_id), result)),
                    )
                extended = z3.If(
                    peer_in_chain,
                    z3.SetAdd(extended, z3.IntVal(peer_charm_id)),
                    extended,
                )
        result = extended

    return result


def _merge_feature_dicts(dicts: Iterable[_FeatureSet]) -> _FeatureSet:
    """Union multiple feature dicts; shared keys are OR'd."""
    merged: _FeatureSet = {}
    for d in dicts:
        for f, v in d.items():
            if f in merged:
                merged[f] = z3.Or(merged[f], v)
            else:
                merged[f] = v
    return merged


def _set_op_features(op: str, left: _FeatureSet, right: _FeatureSet) -> _FeatureSet:
    all_keys = set(left) | set(right)
    result: _FeatureSet = {}
    for f in all_keys:
        l_var: z3.ExprRef = left.get(f, z3.BoolVal(False))
        r_var: z3.ExprRef = right.get(f, z3.BoolVal(False))
        match op:
            case "|":
                result[f] = z3.Or(l_var, r_var)
            case "&":
                result[f] = z3.And(l_var, r_var)
            case "-":
                result[f] = z3.And(l_var, z3.Not(r_var))
    return result


def _channel_set_op(
    op: str, l_val: _LoweredValue, r_val: _LoweredValue, left: AnyExpr, right: AnyExpr, ctx: LoweringContext
) -> _ChannelSet:
    """Apply a set operation to two _ChannelSet values, preserving entry metadata."""
    l_z3 = l_val.z3_set if isinstance(l_val, _ChannelSet) else _lower_as_z3(left, ctx)
    r_z3 = r_val.z3_set if isinstance(r_val, _ChannelSet) else _lower_as_z3(right, ctx)
    l_entries = l_val.entries if isinstance(l_val, _ChannelSet) else []
    r_entries = r_val.entries if isinstance(r_val, _ChannelSet) else []
    kind = l_val.kind if isinstance(l_val, _ChannelSet) else r_val.kind  # type: ignore[union-attr]
    match op:
        case "|":
            return _ChannelSet(z3_set=z3.SetUnion(l_z3, r_z3), entries=l_entries + r_entries, kind=kind)
        case "&":
            return _ChannelSet(z3_set=z3.SetIntersect(l_z3, r_z3), entries=l_entries + r_entries, kind=kind)
        case "-":
            return _ChannelSet(z3_set=z3.SetDifference(l_z3, r_z3), entries=l_entries, kind=kind)
    raise DSLLoweringError(f"Unknown set operator: {op!r}")  # pragma: no cover


def _extract_endpoint_name(expr: AnyExpr) -> str | None:
    """Try to extract a single endpoint name from a CharmsExpr(EndpointExpr(name)) chain.

    Returns None if the expression structure is not a simple single-endpoint reference.
    """
    match expr:
        case CharmsExpr(arg=EndpointExpr(name=name)):
            return name
        case SelfExpr():
            return None
        case _:
            return None


def _lower_in(element: AnyExpr, collection: AnyExpr, ctx: LoweringContext) -> z3.BoolRef:
    """Lower an InExpr (without negation) to a Z3 Bool."""
    elem_lowered = _lower(element, ctx)
    coll_lowered = _lower(collection, ctx)

    # Str in Str: substring containment — not expressible in Z3 without sequence theory;
    # return a fresh unconstrained bool as a conservative approximation.
    if isinstance(elem_lowered, z3.ExprRef) and isinstance(coll_lowered, z3.ExprRef):
        # Both are Z3 scalars; likely Str in Str (substring) — use z3.Contains
        return z3.Contains(coll_lowered, elem_lowered)

    # Str in _FeatureSet: feature membership
    if isinstance(elem_lowered, z3.ExprRef) and isinstance(coll_lowered, dict):
        # Element is a Z3 StringVal — extract the Python string from a StrLit
        if isinstance(element, StrLit):
            return coll_lowered.get(element.value, z3.BoolVal(False))
        raise DSLLoweringError(
            f"'in' with feature set requires a string literal on the left, got {type(element).__name__}"
        )

    # Scalar in _ChannelSet (e.g. "latest" in tracks(charms(ep)))
    if isinstance(elem_lowered, z3.ExprRef) and isinstance(coll_lowered, _ChannelSet):
        return z3.IsMember(elem_lowered, coll_lowered.z3_set)

    # Scalar in IntLiteralSet / StrLiteralSet
    if isinstance(elem_lowered, z3.ExprRef) and isinstance(coll_lowered, z3.ExprRef):
        # Already handled above; this branch unreachable
        pass  # pragma: no cover

    if isinstance(collection, (IntLiteralSet, StrLiteralSet)):
        elem_z3 = _lower_as_z3(element, ctx)
        if isinstance(collection, IntLiteralSet):
            clauses = [elem_z3 == z3.IntVal(v) for v in collection.elements]
        else:
            clauses = [elem_z3 == z3.StringVal(v) for v in collection.elements]
        return z3.Or(clauses) if clauses else z3.BoolVal(False)

    raise DSLLoweringError(
        f"Cannot lower 'in' expression: element={type(element).__name__}, " f"collection={type(collection).__name__}"
    )


def _build_mismatch_tag(
    entry: _ChannelSetEntry,
    kind: str,
    required_value: str | int,
    anchor: CharmPayload,
) -> PeerChannelMismatchTag:
    """Build a PeerChannelMismatchTag for a single mismatched peer.

    anchor  -- the charm whose current value defines the requirement (used by the
               bundle builder as the parent slot when adding the re-fetched charm).
    entry   -- the peer charm that needs to be re-fetched on the required channel.
    """
    kwargs: dict[str, str | int | None] = {
        "required_track": None,
        "required_risk": None,
        "required_channel": None,
        "required_revision": None,
    }
    match kind:
        case "track":
            kwargs["required_track"] = str(required_value)
        case "risk":
            kwargs["required_risk"] = str(required_value)
        case "channel":
            kwargs["required_channel"] = str(required_value)
        case "revision":
            kwargs["required_revision"] = int(required_value)
    return PeerChannelMismatchTag(
        charm=anchor,
        endpoint=entry.endpoint or "",
        peer_charm_name=entry.charm_name,
        peer_charm_id=entry.charm_id,
        **kwargs,
    )


def _emit_channel_mismatch_hints(
    op: str,
    l_lowered: _LoweredValue,
    r_lowered: _LoweredValue,
    ctx: LoweringContext,
) -> None:
    """Emit SubAssertions for channel set comparisons with mismatched peers.

    Two modes:

    Self-anchored -- one side contains {self}. Self's current value is the fixed
    requirement; every active peer on the other side that has a different value
    is blocked, with a hint to re-fetch that peer on self's value.

    Cross-set -- neither side contains {self} (e.g. risks(charms(ep[a])) ==
    risks(charms(ep[b]))). For each entry in src whose value has no active
    counterpart in tgt, a coverage-based blocking condition fires and hints are
    emitted to re-fetch each tgt entry on the missing value. Both directions are
    checked for ==; only the relevant direction for >= / <=.

    Only equality-style operators (==, >=, <=) are handled; strict inequalities
    and != do not imply a specific required value.
    """
    if op not in ("==", ">=", "<="):
        return
    if not isinstance(l_lowered, _ChannelSet) and not isinstance(r_lowered, _ChannelSet):
        return

    self_anchor = CharmPayload(charm_name=ctx.domain_charm.spec.name, charm_id=ctx.charm_id)

    def _self_value(cs: _ChannelSet) -> str | int | None:
        for e in cs.entries:
            if e.charm_id == ctx.charm_id and e.endpoint is None:
                return e.value
        return None

    # Self-anchored path: one side of the comparison contains {self}.
    required: str | int | None = None
    if isinstance(r_lowered, _ChannelSet):
        required = _self_value(r_lowered)
    if required is None and isinstance(l_lowered, _ChannelSet):
        required = _self_value(l_lowered)

    if required is not None:
        # Self's value is fixed; block every active peer that doesn't match it.
        def _emit_for_set(cs: _ChannelSet) -> None:
            for entry in cs.entries:
                if entry.charm_id == ctx.charm_id:
                    continue  # never block self
                if entry.value != required:
                    ctx.sub_assertions.append(
                        SubAssertion(
                            expr=z3.Implies(entry.condition, z3.BoolVal(False)),
                            tag=_build_mismatch_tag(entry, cs.kind, required, self_anchor),
                        )
                    )

        if op in ("==", "<=") and isinstance(l_lowered, _ChannelSet):
            _emit_for_set(l_lowered)
        if op in ("==", ">=") and isinstance(r_lowered, _ChannelSet):
            _emit_for_set(r_lowered)

    elif isinstance(l_lowered, _ChannelSet) and isinstance(r_lowered, _ChannelSet):
        # Cross-set path: no self on either side.
        # For each src entry, if no tgt entry with the same value is active, emit
        # hints to re-fetch every tgt entry on that value. Multiple hints share the
        # same blocking condition so the bundle builder tries all tgt candidates in
        # one solver iteration.
        def _emit_cross(src: _ChannelSet, tgt: _ChannelSet) -> None:
            for entry in src.entries:
                matching = [e for e in tgt.entries if e.value == entry.value]
                coverage: z3.BoolRef = z3.Or([e.condition for e in matching]) if matching else z3.BoolVal(False)
                block_cond: z3.BoolRef = z3.And(entry.condition, z3.Not(coverage))
                anchor = CharmPayload(charm_name=entry.charm_name, charm_id=entry.charm_id)
                for tgt_entry in tgt.entries:
                    ctx.sub_assertions.append(
                        SubAssertion(
                            expr=z3.Implies(block_cond, z3.BoolVal(False)),
                            tag=_build_mismatch_tag(tgt_entry, tgt.kind, entry.value, anchor),
                        )
                    )

        if op in ("==", "<=") and isinstance(l_lowered, _ChannelSet):
            _emit_cross(l_lowered, r_lowered)
        if op in ("==", ">=") and isinstance(r_lowered, _ChannelSet):
            _emit_cross(r_lowered, l_lowered)


def _lower_compare(
    op: str,
    left: AnyExpr,
    right: AnyExpr,
    ctx: LoweringContext,
) -> z3.BoolRef:
    """Lower a CompareExpr to a Z3 Bool."""
    # Lower features expressions eagerly to detect the features==set special case
    # before attempting to lower the StrLiteralSet, which is only valid inside 'in'.
    l_lowered = _lower(left, ctx)
    if isinstance(l_lowered, dict) and isinstance(right, StrLiteralSet):
        return _lower_features_eq(l_lowered, right.elements)
    r_lowered = _lower(right, ctx)
    if isinstance(r_lowered, dict) and isinstance(left, StrLiteralSet):
        return _lower_features_eq(r_lowered, left.elements)

    # Emit sub-assertions for channel set comparisons before producing the Z3 expression.
    _emit_channel_mismatch_hints(op, l_lowered, r_lowered, ctx)

    # Unwrap _ChannelSet to z3.ExprRef for the actual Z3 comparison.
    if isinstance(l_lowered, _ChannelSet):
        l_lowered = l_lowered.z3_set
    if isinstance(r_lowered, _ChannelSet):
        r_lowered = r_lowered.z3_set

    # Set comparisons (CharmSet, SET_STR, SET_INT) all use Z3 subset/equality semantics.
    if left.dsl_type in (DSLType.CHARM_SET, DSLType.SET_STR, DSLType.SET_INT):
        l_z3 = l_lowered if isinstance(l_lowered, z3.ExprRef) else _lower_as_z3(left, ctx)
        r_z3 = r_lowered if isinstance(r_lowered, z3.ExprRef) else _lower_as_z3(right, ctx)
        eq = z3.And(z3.IsSubset(l_z3, r_z3), z3.IsSubset(r_z3, l_z3))
        match op:
            case "==":
                return eq
            case "!=":
                return z3.Not(eq)
            case ">=":
                return z3.IsSubset(r_z3, l_z3)
            case "<=":
                return z3.IsSubset(l_z3, r_z3)
            case ">":
                return z3.And(z3.IsSubset(r_z3, l_z3), z3.Not(eq))
            case "<":
                return z3.And(z3.IsSubset(l_z3, r_z3), z3.Not(eq))

    # Numeric / string / RUNTIME scalar comparisons
    l_z3 = l_lowered if isinstance(l_lowered, z3.ExprRef) else _lower_as_z3(left, ctx)
    r_z3 = r_lowered if isinstance(r_lowered, z3.ExprRef) else _lower_as_z3(right, ctx)
    match op:
        case "==":
            return l_z3 == r_z3
        case "!=":
            return l_z3 != r_z3
        case "<":
            return l_z3 < r_z3
        case "<=":
            return l_z3 <= r_z3
        case ">":
            return l_z3 > r_z3
        case ">=":
            return l_z3 >= r_z3

    raise DSLLoweringError(f"Unknown comparison operator: {op!r}")  # pragma: no cover


def _lower_features_eq(features_dict: _FeatureSet, expected: frozenset[str]) -> z3.ExprRef:
    """Lower features == {literal set} to a Z3 Bool.

    Each feature in expected must be True; every other feature in the dict
    must be False.  If expected contains a feature not in the dict, the result
    is always False (the endpoint can never have that feature).
    """
    conditions: list[z3.ExprRef] = []
    for f, f_var in features_dict.items():
        conditions.append(f_var if f in expected else z3.Not(f_var))
    for f in expected:
        if f not in features_dict:
            conditions.append(z3.BoolVal(False))
    if not conditions:
        return z3.BoolVal(len(expected) == 0)
    return z3.And(conditions)


# ---------------------------------------------------------------------------
# Core lowering dispatcher
# ---------------------------------------------------------------------------


def _lower(expr: AnyExpr, ctx: LoweringContext) -> _LoweredValue:  # noqa: C901
    match expr:
        case IntLit(value=v):
            return z3.IntVal(v)

        case StrLit(value=v):
            return z3.StringVal(v)

        case IntLiteralSet() | StrLiteralSet():
            # Literal sets are only valid as the collection side of InExpr.
            raise DSLLoweringError(f"{type(expr).__name__} cannot appear outside an 'in' expression")

        case SelfExpr():
            return z3.SetAdd(z3.EmptySet(z3.IntSort()), z3.IntVal(ctx.charm_id))

        case EndpointExpr(name=name):
            if name not in ctx.domain_charm.endpoints:
                raise DSLLoweringError(f"Endpoint {name!r} not found on charm {ctx.domain_charm.spec.name!r}")
            return [name]

        case ConfigExpr(key=key):
            cfg = ctx.domain_charm.config.get(key)
            if cfg is not None and cfg.var is not None:
                return cfg.var
            if cfg is not None:
                cfg_val = cfg.default
                if cfg_val is None:
                    raise DSLLoweringError(
                        f"Config key {key!r} on charm {ctx.domain_charm.spec.name!r} has no default value "
                        f"(Charmhub default is null). Add it to the override 'configs' list to use it in a constraint."
                    )
                if isinstance(cfg_val, bool):
                    return z3.BoolVal(cfg_val)
                if isinstance(cfg_val, int):
                    return z3.IntVal(cfg_val)
                if isinstance(cfg_val, float):
                    return z3.RealVal(cfg_val)
                if isinstance(cfg_val, str):
                    return z3.StringVal(cfg_val)
            raise DSLLoweringError(
                f"Config key {key!r} is not declared in charm {ctx.domain_charm.spec.name!r} "
                f"and has no known Charmhub default. "
                f"Declared config keys: {sorted(ctx.domain_charm.config)}"
            )

        case SetConfigExpr(key=key):
            cfg = ctx.domain_charm.config.get(key)
            if cfg is None:
                raise DSLLoweringError(
                    f"Config key {key!r} is not declared in charm {ctx.domain_charm.spec.name!r}. "
                    f"Declared config keys: {sorted(ctx.domain_charm.config)}"
                )
            if cfg.var is not None:
                # If the config key allows None, return the is_set bool; otherwise it is
                # always set (True) whenever the charm exists.
                return cfg.isset_var if cfg.isset_var is not None else z3.BoolVal(True)
            # Key exists on the charm but not in the override configs.
            # If the Charmhub default is null, the value is not set by default.
            # If the Charmhub default is a concrete value, the value is always set.
            return z3.BoolVal(cfg.default is not None)

        case LenExpr(arg=arg):
            endpoints = _lower_as_endpoints(arg, ctx)
            counts = [ctx.domain_charm.endpoints[ep].count for ep in endpoints]
            return z3.Sum(counts + [z3.IntVal(0)])

        case BoolFunc(arg=arg):
            endpoints = _lower_as_endpoints(arg, ctx)
            integrateds: list[z3.ExprRef] = [ctx.domain_charm.endpoints[ep].integrated for ep in endpoints]
            return z3.Or(integrateds) if integrateds else z3.BoolVal(False)

        case CharmsExpr(arg=arg):
            endpoints = _lower_as_endpoints(arg, ctx)
            return _charm_set_for_endpoints(ctx.charm_id, endpoints, ctx.domain)

        case ReachableExpr(arg=EndpointExpr(name=name)):
            return _reachable_set(ctx.charm_id, name, ctx.domain_charm.spec, ctx.domain)

        case FeaturesExpr(arg=arg):
            endpoints = _lower_as_endpoints(arg, ctx)
            return _merge_feature_dicts(ctx.domain_charm.endpoints[ep].features for ep in endpoints)

        case TracksExpr(arg=arg):
            charm_set = _lower_as_z3(arg, ctx)
            endpoint_name = _extract_endpoint_name(arg)
            z3_result: z3.ExprRef = z3.EmptySet(z3.StringSort())
            entries: list[_ChannelSetEntry] = []
            for i, dc in enumerate(ctx.domain.charms):
                condition = z3.And(dc.exists, z3.IsMember(z3.IntVal(i), charm_set))
                z3_result = z3.If(
                    condition, z3.SetAdd(z3_result, z3.StringVal(dc.spec.channel.explicit_track)), z3_result
                )
                entries.append(
                    _ChannelSetEntry(
                        charm_id=i,
                        charm_name=dc.spec.name,
                        value=dc.spec.channel.explicit_track,
                        condition=condition,
                        endpoint=endpoint_name,
                    )
                )
            return _ChannelSet(z3_set=z3_result, entries=entries, kind="track")

        case RisksExpr(arg=arg):
            charm_set = _lower_as_z3(arg, ctx)
            endpoint_name = _extract_endpoint_name(arg)
            z3_result = z3.EmptySet(z3.StringSort())
            entries = []
            for i, dc in enumerate(ctx.domain.charms):
                condition = z3.And(dc.exists, z3.IsMember(z3.IntVal(i), charm_set))
                z3_result = z3.If(condition, z3.SetAdd(z3_result, z3.StringVal(dc.spec.channel.risk)), z3_result)
                entries.append(
                    _ChannelSetEntry(
                        charm_id=i,
                        charm_name=dc.spec.name,
                        value=dc.spec.channel.risk,
                        condition=condition,
                        endpoint=endpoint_name,
                    )
                )
            return _ChannelSet(z3_set=z3_result, entries=entries, kind="risk")

        case ChannelsExpr(arg=arg):
            charm_set = _lower_as_z3(arg, ctx)
            endpoint_name = _extract_endpoint_name(arg)
            z3_result = z3.EmptySet(z3.StringSort())
            entries = []
            for i, dc in enumerate(ctx.domain.charms):
                condition = z3.And(dc.exists, z3.IsMember(z3.IntVal(i), charm_set))
                z3_result = z3.If(condition, z3.SetAdd(z3_result, z3.StringVal(str(dc.spec.channel))), z3_result)
                entries.append(
                    _ChannelSetEntry(
                        charm_id=i,
                        charm_name=dc.spec.name,
                        value=str(dc.spec.channel),
                        condition=condition,
                        endpoint=endpoint_name,
                    )
                )
            return _ChannelSet(z3_set=z3_result, entries=entries, kind="channel")

        case RevisionsExpr(arg=arg):
            charm_set = _lower_as_z3(arg, ctx)
            endpoint_name = _extract_endpoint_name(arg)
            z3_result = z3.EmptySet(z3.IntSort())
            entries = []
            for i, dc in enumerate(ctx.domain.charms):
                condition = z3.And(dc.exists, z3.IsMember(z3.IntVal(i), charm_set))
                z3_result = z3.If(condition, z3.SetAdd(z3_result, z3.IntVal(dc.spec.revision)), z3_result)
                entries.append(
                    _ChannelSetEntry(
                        charm_id=i,
                        charm_name=dc.spec.name,
                        value=dc.spec.revision,
                        condition=condition,
                        endpoint=endpoint_name,
                    )
                )
            return _ChannelSet(z3_set=z3_result, entries=entries, kind="revision")

        case ArithExpr(op=op, left=left, right=right):
            l_z3 = _lower_as_z3(left, ctx)
            r_z3 = _lower_as_z3(right, ctx)
            match op:
                case "+":
                    return l_z3 + r_z3
                case "-":
                    return l_z3 - r_z3
                case "*":
                    return l_z3 * r_z3

        case CompareExpr(op=op, left=left, right=right):
            return _lower_compare(op, left, right, ctx)

        case SetOpExpr(op=op, left=left, right=right, dsl_type=dsl_type):
            if dsl_type == DSLType.RELATION_SET:
                l_eps = _lower_as_endpoints(left, ctx)
                r_eps = _lower_as_endpoints(right, ctx)
                match op:
                    case "|":
                        return l_eps + [e for e in r_eps if e not in l_eps]
                    case "&":
                        return [e for e in l_eps if e in r_eps]
                    case "-":
                        return [e for e in l_eps if e not in r_eps]
            elif dsl_type == DSLType.SET_STR:
                l_val = _lower(left, ctx)
                r_val = _lower(right, ctx)
                if isinstance(l_val, _ChannelSet) or isinstance(r_val, _ChannelSet):
                    return _channel_set_op(op, l_val, r_val, left, right, ctx)
                if isinstance(l_val, dict) and isinstance(r_val, dict):
                    return _set_op_features(op, l_val, r_val)
                raise DSLLoweringError(
                    f"Cannot apply set op {op!r} to {type(l_val).__name__} and {type(r_val).__name__}"
                )
            else:
                # CharmSet or SET_INT: Z3 set operations
                l_val = _lower(left, ctx)
                r_val = _lower(right, ctx)
                if isinstance(l_val, _ChannelSet) or isinstance(r_val, _ChannelSet):
                    return _channel_set_op(op, l_val, r_val, left, right, ctx)
                l_z3 = l_val if isinstance(l_val, z3.ExprRef) else _lower_as_z3(left, ctx)
                r_z3 = r_val if isinstance(r_val, z3.ExprRef) else _lower_as_z3(right, ctx)
                match op:
                    case "|":
                        return z3.SetUnion(l_z3, r_z3)
                    case "&":
                        return z3.SetIntersect(l_z3, r_z3)
                    case "-":
                        return z3.SetDifference(l_z3, r_z3)

        case InExpr(element=element, collection=collection, negated=negated):
            # Handle literal set collections before general lowering
            if isinstance(collection, (IntLiteralSet, StrLiteralSet)):
                elem_z3 = _lower_as_z3(element, ctx)
                if isinstance(collection, IntLiteralSet):
                    clauses: list[z3.ExprRef] = [elem_z3 == z3.IntVal(v) for v in collection.elements]
                else:
                    clauses = [elem_z3 == z3.StringVal(v) for v in collection.elements]
                result: z3.ExprRef = z3.Or(clauses) if clauses else z3.BoolVal(False)
                return z3.Not(result) if negated else result

            # Feature membership: "str" in features(endpoint[x])
            if isinstance(element, StrLit) and collection.dsl_type == DSLType.SET_STR:
                features_dict = _lower_as_features(collection, ctx)
                membership: z3.ExprRef = features_dict.get(element.value, z3.BoolVal(False))
                return z3.Not(membership) if negated else membership

            # General scalar in collection (e.g. config in runtime set)
            result = _lower_in(element, collection, ctx)
            return z3.Not(result) if negated else result

        case NotExpr(arg=arg):
            return z3.Not(_lower_as_z3(arg, ctx))

        case AndExpr(left=left, right=right):
            return z3.And(_lower_as_z3(left, ctx), _lower_as_z3(right, ctx))

        case OrExpr(left=left, right=right):
            return z3.Or(_lower_as_z3(left, ctx), _lower_as_z3(right, ctx))

        case ImpliesExpr(antecedent=antecedent, consequent=consequent):
            return z3.Implies(_lower_as_z3(antecedent, ctx), _lower_as_z3(consequent, ctx))

    raise DSLLoweringError(f"Unhandled node type: {type(expr).__name__}")  # pragma: no cover
