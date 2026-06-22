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

"""Unit tests for the constraint DSL parser."""

import pytest
from pydantic.dataclasses import dataclass

from bundle_builder_x.constraints_dsl import (
    AndExpr,
    ArithExpr,
    BoolFunc,
    CharmsExpr,
    CompareExpr,
    ConfigExpr,
    DSLSyntaxError,
    DSLType,
    DSLTypeError,
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
    ResourceExpr,
    SelfExpr,
    SetOpExpr,
    SetResourceExpr,
    StrLit,
    StrLiteralSet,
    UnitsExpr,
    parse_constraint,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _endpoint(name: str) -> EndpointExpr:
    return EndpointExpr(name=name)


def _len(name: str) -> LenExpr:
    return LenExpr(arg=_endpoint(name))


def _bool(name: str) -> BoolFunc:
    return BoolFunc(arg=_endpoint(name))


def _features(name: str) -> FeaturesExpr:
    return FeaturesExpr(arg=_endpoint(name))


def _str_in_features(s: str, ep: str) -> InExpr:
    return InExpr(element=StrLit(value=s), collection=_features(ep))


# ---------------------------------------------------------------------------
# Atom parsing
# ---------------------------------------------------------------------------


class TestAtoms:
    @dataclass
    class Params:
        label: str
        text: str
        expected: object

    test_cases = [
        Params(label="int_lit", text="42", expected=IntLit(value=42)),
        Params(label="str_lit", text='"hello"', expected=StrLit(value="hello")),
        Params(label="endpoint", text="endpoint[database]", expected=EndpointExpr(name="database")),
        Params(
            label="endpoint_hyphenated",
            text="endpoint[receive-ca-cert]",
            expected=EndpointExpr(name="receive-ca-cert"),
        ),
        Params(label="config", text="config[dev]", expected=ConfigExpr(key="dev")),
        Params(
            label="config_hyphenated",
            text="config[num-history-shards]",
            expected=ConfigExpr(key="num-history-shards"),
        ),
        Params(label="resource", text="resource[my-image]", expected=ResourceExpr(key="my-image")),
        Params(
            label="resource_hyphenated",
            text="resource[temporal-worker-image]",
            expected=ResourceExpr(key="temporal-worker-image"),
        ),
        Params(
            label="set_resource",
            text="set(resource[my-image])",
            expected=SetResourceExpr(key="my-image"),
        ),
        Params(label="self_expr", text="{self}", expected=SelfExpr()),
        Params(
            label="int_set_singleton",
            text="{1}",
            expected=IntLiteralSet(elements=frozenset({1})),
        ),
        Params(
            label="int_set_multi",
            text="{1, 2, 4}",
            expected=IntLiteralSet(elements=frozenset({1, 2, 4})),
        ),
        Params(
            label="str_set_singleton",
            text='{"admin"}',
            expected=StrLiteralSet(elements=frozenset({"admin"})),
        ),
        Params(
            label="str_set_multi",
            text='{"a", "b"}',
            expected=StrLiteralSet(elements=frozenset({"a", "b"})),
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        assert parse_constraint(params.text) == params.expected


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


class TestArithmetic:
    @dataclass
    class Params:
        label: str
        text: str
        expected: object

    test_cases = [
        Params(
            label="add",
            text="len(endpoint[a]) + len(endpoint[b])",
            expected=ArithExpr(op="+", left=_len("a"), right=_len("b")),
        ),
        Params(
            label="mul",
            text="2 * len(endpoint[a])",
            expected=ArithExpr(op="*", left=IntLit(value=2), right=_len("a")),
        ),
        Params(
            label="sub_via_set_op_rewritten",
            text="len(endpoint[a]) - 1",
            expected=ArithExpr(op="-", left=_len("a"), right=IntLit(value=1)),
        ),
        Params(
            label="chained_add",
            text="len(endpoint[a]) + len(endpoint[b]) + len(endpoint[c])",
            expected=ArithExpr(
                op="+",
                left=ArithExpr(op="+", left=_len("a"), right=_len("b")),
                right=_len("c"),
            ),
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        assert parse_constraint(params.text) == params.expected


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------


class TestComparisons:
    @dataclass
    class Params:
        label: str
        text: str
        expected: object

    test_cases = [
        Params(
            label="eq_int",
            text="len(endpoint[database]) == 1",
            expected=CompareExpr(op="==", left=_len("database"), right=IntLit(value=1)),
        ),
        Params(
            label="neq",
            text="len(endpoint[a]) != 0",
            expected=CompareExpr(op="!=", left=_len("a"), right=IntLit(value=0)),
        ),
        Params(
            label="ge_int",
            text="len(endpoint[a]) >= 1",
            expected=CompareExpr(op=">=", left=_len("a"), right=IntLit(value=1)),
        ),
        Params(
            label="le_int",
            text="len(endpoint[a]) <= 1",
            expected=CompareExpr(op="<=", left=_len("a"), right=IntLit(value=1)),
        ),
        Params(
            label="gt_int",
            text="len(endpoint[a]) > 0",
            expected=CompareExpr(op=">", left=_len("a"), right=IntLit(value=0)),
        ),
        Params(
            label="lt_int",
            text="len(endpoint[a]) < 2",
            expected=CompareExpr(op="<", left=_len("a"), right=IntLit(value=2)),
        ),
        Params(
            label="eq_str_set",
            text='features(endpoint[admin]) == {"admin"}',
            expected=CompareExpr(
                op="==",
                left=_features("admin"),
                right=StrLiteralSet(elements=frozenset({"admin"})),
            ),
        ),
        Params(
            label="ge_charm_set",
            text="reachable(endpoint[receive-ca-cert]) >= charms(endpoint[oauth])",
            expected=CompareExpr(
                op=">=",
                left=ReachableExpr(arg=_endpoint("receive-ca-cert")),
                right=CharmsExpr(arg=_endpoint("oauth")),
            ),
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        assert parse_constraint(params.text) == params.expected


# ---------------------------------------------------------------------------
# 'in' / 'not in'
# ---------------------------------------------------------------------------


class TestInOperator:
    @dataclass
    class Params:
        label: str
        text: str
        expected: object

    test_cases = [
        Params(
            label="str_in_features",
            text='"tls" in features(endpoint[database])',
            expected=InExpr(element=StrLit(value="tls"), collection=_features("database"), negated=False),
        ),
        Params(
            label="str_not_in_features",
            text='"tls" not in features(endpoint[database])',
            expected=InExpr(element=StrLit(value="tls"), collection=_features("database"), negated=True),
        ),
        Params(
            label="config_in_int_set",
            text="config[num-history-shards] in {1, 2, 4}",
            expected=InExpr(
                element=ConfigExpr(key="num-history-shards"),
                collection=IntLiteralSet(elements=frozenset({1, 2, 4})),
                negated=False,
            ),
        ),
        Params(
            label="config_not_in_int_set",
            text="config[num-history-shards] not in {1, 2, 4}",
            expected=InExpr(
                element=ConfigExpr(key="num-history-shards"),
                collection=IntLiteralSet(elements=frozenset({1, 2, 4})),
                negated=True,
            ),
        ),
        Params(
            label="str_substring_in_str",
            text='"foo" in "foobar"',
            expected=InExpr(element=StrLit(value="foo"), collection=StrLit(value="foobar"), negated=False),
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        assert parse_constraint(params.text) == params.expected


# ---------------------------------------------------------------------------
# Set operations
# ---------------------------------------------------------------------------


class TestSetOps:
    @dataclass
    class Params:
        label: str
        text: str
        expected: object

    test_cases = [
        Params(
            label="union_relation_set",
            text="endpoint[a] | endpoint[b]",
            expected=SetOpExpr(op="|", left=_endpoint("a"), right=_endpoint("b"), dsl_type=DSLType.RELATION_SET),
        ),
        Params(
            label="intersect_charm_set",
            text="{self} & charms(endpoint[a])",
            expected=SetOpExpr(
                op="&",
                left=SelfExpr(),
                right=CharmsExpr(arg=_endpoint("a")),
                dsl_type=DSLType.CHARM_SET,
            ),
        ),
        Params(
            label="union_for_features_arg",
            text='"tls" in features(endpoint[traefik-route] | endpoint[ingress] | endpoint[ingress-per-unit])',
            expected=InExpr(
                element=StrLit(value="tls"),
                collection=FeaturesExpr(
                    arg=SetOpExpr(
                        op="|",
                        left=SetOpExpr(
                            op="|",
                            left=_endpoint("traefik-route"),
                            right=_endpoint("ingress"),
                            dsl_type=DSLType.RELATION_SET,
                        ),
                        right=_endpoint("ingress-per-unit"),
                        dsl_type=DSLType.RELATION_SET,
                    )
                ),
                negated=False,
            ),
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        assert parse_constraint(params.text) == params.expected


# ---------------------------------------------------------------------------
# Boolean operators
# ---------------------------------------------------------------------------


class TestBooleanOps:
    @dataclass
    class Params:
        label: str
        text: str
        expected: object

    test_cases = [
        Params(
            label="implies",
            text='bool(endpoint[a]) => features(endpoint[a]) == {"admin"}',
            expected=ImpliesExpr(
                antecedent=_bool("a"),
                consequent=CompareExpr(
                    op="==",
                    left=_features("a"),
                    right=StrLiteralSet(elements=frozenset({"admin"})),
                ),
            ),
        ),
        Params(
            label="implies_right_assoc",
            text="bool(endpoint[a]) => bool(endpoint[b]) => bool(endpoint[c])",
            expected=ImpliesExpr(
                antecedent=_bool("a"),
                consequent=ImpliesExpr(antecedent=_bool("b"), consequent=_bool("c")),
            ),
        ),
        Params(
            label="and",
            text="bool(endpoint[a]) and bool(endpoint[b])",
            expected=AndExpr(left=_bool("a"), right=_bool("b")),
        ),
        Params(
            label="or",
            text="bool(endpoint[a]) or bool(endpoint[b])",
            expected=OrExpr(left=_bool("a"), right=_bool("b")),
        ),
        Params(
            label="not",
            text="not bool(endpoint[a])",
            expected=NotExpr(arg=_bool("a")),
        ),
        Params(
            label="not_config_implies",
            text='not config[dev] => "tls" in features(endpoint[public-ingress])',
            expected=ImpliesExpr(
                antecedent=NotExpr(arg=ConfigExpr(key="dev")),
                consequent=_str_in_features("tls", "public-ingress"),
            ),
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        assert parse_constraint(params.text) == params.expected


# ---------------------------------------------------------------------------
# Real override constraints (smoke tests)
# ---------------------------------------------------------------------------


class TestRealConstraints:
    """Smoke tests: parse every constraint found in static/charm-overrides/*.yaml."""

    @dataclass
    class Params:
        label: str
        text: str

    test_cases = [
        # canonical-livepatch-server-k8s
        Params(
            label="livepatch_exactly_one_db",
            text="len(endpoint[database-legacy]) + len(endpoint[database]) == 1",
        ),
        # pgbouncer-k8s
        Params(
            label="pgbouncer_at_least_one_db",
            text="len(endpoint[database]) + len(endpoint[db]) + len(endpoint[db-admin]) >= 1",
        ),
        Params(
            label="pgbouncer_tls_database",
            text='"tls" in features(endpoint[database]) => bool(endpoint[certificates])',
        ),
        Params(
            label="pgbouncer_tls_db",
            text='"tls" in features(endpoint[db]) => bool(endpoint[certificates])',
        ),
        Params(
            label="pgbouncer_tls_db_admin",
            text='"tls" in features(endpoint[db-admin]) => bool(endpoint[certificates])',
        ),
        # postgresql-k8s
        Params(
            label="postgresql_at_most_one_db",
            text="len(endpoint[database]) + len(endpoint[db]) + len(endpoint[db-admin]) <= 1",
        ),
        # temporal-k8s
        Params(
            label="temporal_config_in_set",
            text="config[num-history-shards] in {1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024}",
        ),
        Params(
            label="temporal_admin_feature_pin",
            text='bool(endpoint[admin]) => features(endpoint[admin]) == {"admin"}',
        ),
        Params(
            label="temporal_ui_feature_pin",
            text='bool(endpoint[ui]) => features(endpoint[ui]) == {"ui"}',
        ),
        # juju-jimm-k8s
        Params(
            label="jimm_tls_reachable",
            text='"tls" in features(endpoint[oauth]) => reachable(endpoint[receive-ca-cert]) >= charms(endpoint[oauth])',
        ),
        # hydra - older track
        Params(
            label="hydra_ingress_tls_implies_oauth_tls",
            text='"tls" in features(endpoint[public-ingress]) => "tls" in features(endpoint[oauth])',
        ),
        Params(
            label="hydra_oauth_tls_implies_ingress_tls",
            text='"tls" in features(endpoint[oauth]) => "tls" in features(endpoint[public-ingress])',
        ),
        Params(
            label="hydra_not_dev_implies_tls",
            text='not config[dev] => "tls" in features(endpoint[public-ingress])',
        ),
        # traefik-k8s
        Params(
            label="traefik_any_tls_requires_certs",
            text='"tls" in features(endpoint[traefik-route] | endpoint[ingress] | endpoint[ingress-per-unit]) => bool(endpoint[certificates])',
        ),
        # temporal-worker-k8s
        Params(
            label="temporal_worker_namespace_and_queue_set",
            text="set(config[namespace]) and set(config[queue])",
        ),
        Params(
            label="temporal_worker_db_implies_db_name",
            text="bool(endpoint[database]) => set(config[db-name])",
        ),
        Params(
            label="temporal_worker_image_set",
            text="set(resource[temporal-worker-image])",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        # Asserts no exception; structural correctness covered by other tests
        result = parse_constraint(params.text)
        assert result is not None


# ---------------------------------------------------------------------------
# Precedence and parentheses
# ---------------------------------------------------------------------------


class TestPrecedence:
    @dataclass
    class Params:
        label: str
        text: str
        expected: object

    test_cases = [
        Params(
            label="add_before_compare",
            text="len(endpoint[a]) + len(endpoint[b]) == 1",
            expected=CompareExpr(
                op="==",
                left=ArithExpr(op="+", left=_len("a"), right=_len("b")),
                right=IntLit(value=1),
            ),
        ),
        Params(
            label="parens_override_precedence",
            text="(len(endpoint[a]) == 1) and (len(endpoint[b]) == 1)",
            expected=AndExpr(
                left=CompareExpr(op="==", left=_len("a"), right=IntLit(value=1)),
                right=CompareExpr(op="==", left=_len("b"), right=IntLit(value=1)),
            ),
        ),
        Params(
            label="and_before_or",
            text="bool(endpoint[a]) and bool(endpoint[b]) or bool(endpoint[c])",
            expected=OrExpr(
                left=AndExpr(left=_bool("a"), right=_bool("b")),
                right=_bool("c"),
            ),
        ),
        Params(
            label="implies_lower_than_or",
            text="bool(endpoint[a]) or bool(endpoint[b]) => bool(endpoint[c])",
            expected=ImpliesExpr(
                antecedent=OrExpr(left=_bool("a"), right=_bool("b")),
                consequent=_bool("c"),
            ),
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        assert parse_constraint(params.text) == params.expected


# ---------------------------------------------------------------------------
# Type errors
# ---------------------------------------------------------------------------


class TestTypeErrors:
    @dataclass
    class Params:
        label: str
        text: str
        fragment: str  # substring expected in error message

    test_cases = [
        Params(
            label="len_of_non_relation_set",
            text="len({self})",
            fragment="RelationSet",
        ),
        Params(
            label="bool_of_non_relation_set",
            text='bool("hello")',
            fragment="RelationSet",
        ),
        Params(
            label="charms_of_non_relation_set",
            text="charms({self})",
            fragment="RelationSet",
        ),
        Params(
            label="features_of_non_relation_set",
            text="features({self})",
            fragment="RelationSet",
        ),
        Params(
            label="reachable_of_non_endpoint",
            text="reachable(charms(endpoint[a]))",
            fragment=r"endpoint\[name\]",
        ),
        Params(
            label="tracks_of_non_charm_set",
            text="tracks(endpoint[a])",
            fragment="CharmSet",
        ),
        Params(
            label="arith_on_str",
            text='"hello" + "world"',
            fragment="Int",
        ),
        Params(
            label="compare_lt_on_sets",
            text="features(endpoint[a]) < features(endpoint[b])",
            fragment="Int",
        ),
        Params(
            label="compare_mismatched_types",
            text="len(endpoint[a]) == {self}",
            fragment="matching types",
        ),
        Params(
            label="set_op_mixed_types",
            text="endpoint[a] | {self}",
            fragment="matching set types",
        ),
        Params(
            label="set_sub_non_set",
            text='"hello" - "world"',
            fragment="Int",
        ),
        Params(
            label="in_wrong_collection_type",
            text='"tls" in endpoint[a]',
            fragment="set or Str",
        ),
        Params(
            label="not_on_non_bool",
            text="not len(endpoint[a])",
            fragment="Bool",
        ),
        Params(
            label="and_non_bool_left",
            text="len(endpoint[a]) and bool(endpoint[b])",
            fragment="Bool",
        ),
        Params(
            label="or_non_bool_right",
            text="bool(endpoint[a]) or len(endpoint[b])",
            fragment="Bool",
        ),
        Params(
            label="implies_non_bool_antecedent",
            text="len(endpoint[a]) => bool(endpoint[b])",
            fragment="Bool antecedent",
        ),
        Params(
            label="implies_non_bool_consequent",
            text="bool(endpoint[a]) => len(endpoint[b])",
            fragment="Bool consequent",
        ),
        Params(
            label="int_in_str_set",
            text='1 in {"a", "b"}',
            fragment="Str",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        with pytest.raises(DSLTypeError, match=params.fragment):
            parse_constraint(params.text)


# ---------------------------------------------------------------------------
# Syntax errors
# ---------------------------------------------------------------------------


class TestSyntaxErrors:
    @dataclass
    class Params:
        label: str
        text: str

    test_cases = [
        Params(label="unexpected_char", text="endpoint[a] @ 1"),
        Params(label="missing_rbracket", text="endpoint[a"),
        Params(label="missing_rparen", text="len(endpoint[a]"),
        Params(label="trailing_token", text="len(endpoint[a]) == 1 extra"),
        Params(label="empty_string", text=""),
        Params(label="bare_ident", text="foobar"),
        Params(label="mixed_set_elements", text='{"a", 1}'),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        with pytest.raises((DSLSyntaxError, DSLTypeError)):
            parse_constraint(params.text)


# ---------------------------------------------------------------------------
# units() function
# ---------------------------------------------------------------------------


class TestUnitsExpr:
    """Tests for the units(charm_set) DSL function and len(units(...)) composition."""

    def test_units_self_parses(self) -> None:
        # GIVEN the expression units({self})
        # WHEN parsed
        result = parse_constraint("units({self})")

        # THEN it produces UnitsExpr(SelfExpr()) with UNIT_SET type
        assert result == UnitsExpr(arg=SelfExpr())
        assert result.dsl_type == DSLType.UNIT_SET

    def test_len_units_self_parses(self) -> None:
        # GIVEN len(units({self}))
        # WHEN parsed
        result = parse_constraint("len(units({self}))")

        # THEN it produces LenExpr(UnitsExpr(SelfExpr())) with INT type
        assert result == LenExpr(arg=UnitsExpr(arg=SelfExpr()))
        assert result.dsl_type == DSLType.INT

    def test_len_units_self_ge(self) -> None:
        # GIVEN the opensearch HA constraint
        # WHEN parsed
        result = parse_constraint("len(units({self})) >= 3")

        # THEN it produces a CompareExpr wrapping LenExpr(UnitsExpr(...))
        assert result == CompareExpr(
            op=">=",
            left=LenExpr(arg=UnitsExpr(arg=SelfExpr())),
            right=IntLit(value=3),
        )

    def test_units_requires_charm_set(self) -> None:
        # GIVEN units() with a non-CharmSet argument (an integer literal)
        # WHEN parsed and type-checked
        # THEN a DSLTypeError is raised
        from bundle_builder_x.constraints_dsl import DSLTypeError

        with pytest.raises(DSLTypeError, match="CharmSet"):
            parse_constraint("units(3)")

    def test_set_with_self_raises_syntax_error(self) -> None:
        # GIVEN set({self}) which is not a supported use of set()
        # WHEN parsed
        # THEN a DSLSyntaxError is raised (set only supports config/resource args)
        from bundle_builder_x.constraints_dsl import DSLSyntaxError

        with pytest.raises(DSLSyntaxError):
            parse_constraint("set({self})")

    def test_units_equality_raises_type_error(self) -> None:
        # GIVEN units({self}) == units({self}) — UnitSet cannot be used in == comparisons
        # WHEN parsed and type-checked
        # THEN a DSLTypeError is raised
        from bundle_builder_x.constraints_dsl import DSLTypeError

        with pytest.raises(DSLTypeError, match="UnitSet"):
            parse_constraint("units({self}) == units({self})")

    def test_len_units_real_override(self) -> None:
        # GIVEN the exact constraint from static/charm-overrides/opensearch.yaml
        # WHEN parsed
        result = parse_constraint("len(units({self})) >= 3")

        # THEN no exception and result is a boolean comparison
        assert result.dsl_type == DSLType.BOOL
