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

"""Constraint DSL parser.

Parses per-charm integration constraint strings (as written in override YAML files)
into a typed intermediate representation (BaseModel AST). The IR is then lowered to
Z3 expressions by a separate lowering pass.

Grammar (in precedence order, lowest to highest)::

    constraint   = implies_expr
    implies_expr = or_expr ('=>' or_expr)*       right-associative
    or_expr      = and_expr ('or' and_expr)*
    and_expr     = not_expr ('and' not_expr)*
    not_expr     = 'not' not_expr | in_expr
    in_expr      = compare_expr (('in' | 'not' 'in') compare_expr)?
    compare_expr = set_op_expr (('==' | '!=' | '<' | '<=' | '>' | '>=') set_op_expr)?
    set_op_expr  = add_expr (('|' | '&' | '-') add_expr)*
    add_expr     = mul_expr (('+') mul_expr)*
    mul_expr     = atom ('*' atom)*
    atom         = '(' constraint ')'
                 | 'endpoint' '[' NAME ']'
                 | 'len' '(' constraint ')'
                 | 'bool' '(' constraint ')'
                 | 'charms' '(' constraint ')'
                 | 'reachable' '(' constraint ')'
                 | 'features' '(' constraint ')'
                 | 'tracks' '(' constraint ')'
                 | 'risks' '(' constraint ')'
                 | 'channels' '(' constraint ')'
                 | 'revisions' '(' constraint ')'
                 | 'units' '(' constraint ')'
                 | 'config' '[' NAME ']'
                 | 'resource' '[' NAME ']'
                 | '{' 'self' '}'
                 | '{' literal (',' literal)* '}'
                 | STRING
                 | INTEGER

Note: '-' is handled in set_op_expr only. The type checker disambiguates arithmetic
subtraction (both operands INT) from set subtraction (both operands same set type).
"""

import re
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JUJU_CONSTRAINT_KEYS: frozenset[str] = frozenset({"cores", "mem", "root-disk"})

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DSLSyntaxError(ValueError):
    """Raised when the constraint string cannot be tokenized or parsed."""


class DSLTypeError(ValueError):
    """Raised when an operator receives operands of incompatible types."""


# ---------------------------------------------------------------------------
# DSLType
# ---------------------------------------------------------------------------


class DSLType(str, Enum):
    INT = "Int"
    BOOL = "Bool"
    STR = "Str"
    RELATION_SET = "RelationSet"
    CHARM_SET = "CharmSet"
    UNIT_SET = "UnitSet"
    SET_STR = "Set[Str]"
    SET_INT = "Set[Int]"
    RUNTIME = "Runtime"  # ConfigExpr/ResourceExpr: type resolved at Z3 lowering time
    PENDING = "Pending"  # SetOpExpr: resolved by _check_types; never in final AST


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


class TokenKind(str, Enum):
    IDENT = "IDENT"
    INT = "INT"
    STR = "STR"
    LBRACKET = "LBRACKET"
    RBRACKET = "RBRACKET"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    LBRACE = "LBRACE"
    RBRACE = "RBRACE"
    COMMA = "COMMA"
    PLUS = "PLUS"
    MINUS = "MINUS"
    STAR = "STAR"
    PIPE = "PIPE"
    AMP = "AMP"
    EQ = "EQ"
    NEQ = "NEQ"
    LT = "LT"
    LE = "LE"
    GT = "GT"
    GE = "GE"
    IMPLIES = "IMPLIES"
    EOF = "EOF"


class Token(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: TokenKind
    value: str
    pos: int


_TOKEN_RE = re.compile(
    r"""
    (?P<WHITESPACE>[ \t\n]+)
  | (?P<IMPLIES>=>)
  | (?P<EQ>==)
  | (?P<NEQ>!=)
  | (?P<GE>>=)
  | (?P<LE><=)
  | (?P<GT>>)
  | (?P<LT><)
  | (?P<LBRACKET>\[)
  | (?P<RBRACKET>])
  | (?P<LPAREN>\()
  | (?P<RPAREN>\))
  | (?P<LBRACE>\{)
  | (?P<RBRACE>})
  | (?P<COMMA>,)
  | (?P<PLUS>\+)
  | (?P<MINUS>-)
  | (?P<STAR>\*)
  | (?P<PIPE>\|)
  | (?P<AMP>&)
  | (?P<STR>"(?:[^"\\]|\\.)*")
  | (?P<INT>-?[0-9]+)
  | (?P<IDENT>[a-zA-Z_][a-zA-Z0-9_-]*)
""",
    re.VERBOSE,
)


def _tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    length = len(text)
    while pos < length:
        m = _TOKEN_RE.match(text, pos)
        if m is None:
            raise DSLSyntaxError(f"Unexpected character {text[pos]!r} at position {pos}")
        pos = m.end()
        kind_name = m.lastgroup
        if kind_name is None:
            raise DSLSyntaxError(f"Unrecognised token at position {m.start()}")
        if kind_name == "WHITESPACE":
            continue
        if kind_name == "STR":
            # Strip surrounding double quotes
            tokens.append(Token(kind=TokenKind.STR, value=m.group()[1:-1], pos=m.start()))
        else:
            tokens.append(Token(kind=TokenKind[kind_name], value=m.group(), pos=m.start()))
    tokens.append(Token(kind=TokenKind.EOF, value="", pos=pos))
    return tokens


# ---------------------------------------------------------------------------
# AST node classes
# ---------------------------------------------------------------------------


class IntLit(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["int_lit"] = "int_lit"
    dsl_type: Literal[DSLType.INT] = DSLType.INT
    value: int


class StrLit(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["str_lit"] = "str_lit"
    dsl_type: Literal[DSLType.STR] = DSLType.STR
    value: str


class IntLiteralSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["int_set"] = "int_set"
    dsl_type: Literal[DSLType.SET_INT] = DSLType.SET_INT
    elements: frozenset[int]


class StrLiteralSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["str_set"] = "str_set"
    dsl_type: Literal[DSLType.SET_STR] = DSLType.SET_STR
    elements: frozenset[str]


class SelfExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["self"] = "self"
    dsl_type: Literal[DSLType.CHARM_SET] = DSLType.CHARM_SET


class EndpointExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["endpoint"] = "endpoint"
    dsl_type: Literal[DSLType.RELATION_SET] = DSLType.RELATION_SET
    name: str


class ConfigExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["config"] = "config"
    dsl_type: Literal[DSLType.RUNTIME] = DSLType.RUNTIME
    key: str


class SetConfigExpr(BaseModel):
    """set(config[key]) - True when the config key is set to one of its allowed values.

    This is False only when the config key allows None (unset) and the solver
    has chosen to leave it unset.  When the allowed values list does not include
    None, the config is always set and this expression is trivially True.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["set_config"] = "set_config"
    dsl_type: Literal[DSLType.BOOL] = DSLType.BOOL
    key: str


class ResourceExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["resource"] = "resource"
    dsl_type: Literal[DSLType.RUNTIME] = DSLType.RUNTIME
    key: str


class SetResourceExpr(BaseModel):
    """set(resource[key]) - True when the resource key is set.

    Mirrors SetConfigExpr for resources.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["set_resource"] = "set_resource"
    dsl_type: Literal[DSLType.BOOL] = DSLType.BOOL
    key: str


class LenExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["len"] = "len"
    dsl_type: Literal[DSLType.INT] = DSLType.INT
    arg: "AnyExpr"


class BoolFunc(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["bool_func"] = "bool_func"
    dsl_type: Literal[DSLType.BOOL] = DSLType.BOOL
    arg: "AnyExpr"


class CharmsExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["charms"] = "charms"
    dsl_type: Literal[DSLType.CHARM_SET] = DSLType.CHARM_SET
    arg: "AnyExpr"


class FeaturesExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["features"] = "features"
    dsl_type: Literal[DSLType.SET_STR] = DSLType.SET_STR
    arg: "AnyExpr"


class ReachableExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["reachable"] = "reachable"
    dsl_type: Literal[DSLType.CHARM_SET] = DSLType.CHARM_SET
    arg: "AnyExpr"


class TracksExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["tracks"] = "tracks"
    dsl_type: Literal[DSLType.SET_STR] = DSLType.SET_STR
    arg: "AnyExpr"


class RisksExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["risks"] = "risks"
    dsl_type: Literal[DSLType.SET_STR] = DSLType.SET_STR
    arg: "AnyExpr"


class ChannelsExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["channels"] = "channels"
    dsl_type: Literal[DSLType.SET_STR] = DSLType.SET_STR
    arg: "AnyExpr"


class RevisionsExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["revisions"] = "revisions"
    dsl_type: Literal[DSLType.SET_INT] = DSLType.SET_INT
    arg: "AnyExpr"


class UnitsExpr(BaseModel):
    """units(charm_set) — the set of Juju units for a charm set.

    Used with ``len()`` to constrain how many units a charm is deployed with::

        len(units({self})) >= 3   # require at least 3 units (e.g. OpenSearch HA)

    The argument must be a ``CharmSet`` expression.  The most common form is
    ``{self}`` (the current application), but any ``CharmSet`` is accepted for
    forward-compatibility with multi-charm summation.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["units"] = "units"
    dsl_type: Literal[DSLType.UNIT_SET] = DSLType.UNIT_SET
    arg: "AnyExpr"


class JujuConstraintExpr(BaseModel):
    """juju_constraint[key] — a Juju resource constraint dimension for this charm.

    Returns an ``Int`` representing the constraint value for the named dimension.
    Use with comparison operators to declare resource floors::

        juju_constraint[cores] >= 2
        juju_constraint[mem] >= 4096        # MB
        juju_constraint[root-disk] >= 20480 # MB

    On machine/VM clouds the exported value is a *minimum* (Juju finds a machine
    meeting at least that spec).  On Kubernetes it is a *cap* (Juju limits the
    container to at most that value).  In both cases the optimizer minimises to
    the smallest value that satisfies all constraints, so declaring a floor also
    produces the tightest cap for container environments.

    Valid keys: ``cores``, ``mem``, ``root-disk``.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["juju_constraint"] = "juju_constraint"
    dsl_type: Literal[DSLType.INT] = DSLType.INT
    key: str


class ArithExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["arith"] = "arith"
    dsl_type: Literal[DSLType.INT] = DSLType.INT
    op: Literal["+", "-", "*"]
    left: "AnyExpr"
    right: "AnyExpr"


class CompareExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["compare"] = "compare"
    dsl_type: Literal[DSLType.BOOL] = DSLType.BOOL
    op: Literal["==", "!=", "<", "<=", ">", ">="]
    left: "AnyExpr"
    right: "AnyExpr"


class SetOpExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["set_op"] = "set_op"
    # PENDING in the raw parse tree; always replaced with a concrete set type by _check_types.
    dsl_type: DSLType = DSLType.PENDING
    op: Literal["|", "&", "-"]
    left: "AnyExpr"
    right: "AnyExpr"


class InExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["in"] = "in"
    dsl_type: Literal[DSLType.BOOL] = DSLType.BOOL
    element: "AnyExpr"
    collection: "AnyExpr"
    negated: bool = False


class NotExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["not"] = "not"
    dsl_type: Literal[DSLType.BOOL] = DSLType.BOOL
    arg: "AnyExpr"


class AndExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["and"] = "and"
    dsl_type: Literal[DSLType.BOOL] = DSLType.BOOL
    left: "AnyExpr"
    right: "AnyExpr"


class OrExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["or"] = "or"
    dsl_type: Literal[DSLType.BOOL] = DSLType.BOOL
    left: "AnyExpr"
    right: "AnyExpr"


class ImpliesExpr(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["implies"] = "implies"
    dsl_type: Literal[DSLType.BOOL] = DSLType.BOOL
    antecedent: "AnyExpr"
    consequent: "AnyExpr"


# ---------------------------------------------------------------------------
# Union type — must be defined after all node classes
# ---------------------------------------------------------------------------

AnyExpr = Annotated[
    Union[
        IntLit,
        StrLit,
        IntLiteralSet,
        StrLiteralSet,
        SelfExpr,
        EndpointExpr,
        ConfigExpr,
        SetConfigExpr,
        ResourceExpr,
        SetResourceExpr,
        JujuConstraintExpr,
        LenExpr,
        BoolFunc,
        CharmsExpr,
        FeaturesExpr,
        ReachableExpr,
        TracksExpr,
        RisksExpr,
        ChannelsExpr,
        RevisionsExpr,
        UnitsExpr,
        ArithExpr,
        CompareExpr,
        SetOpExpr,
        InExpr,
        NotExpr,
        AndExpr,
        OrExpr,
        ImpliesExpr,
    ],
    Field(discriminator="kind"),
]

# Resolve forward references now that AnyExpr is defined
SetConfigExpr.model_rebuild()
SetResourceExpr.model_rebuild()
JujuConstraintExpr.model_rebuild()
LenExpr.model_rebuild()
BoolFunc.model_rebuild()
CharmsExpr.model_rebuild()
FeaturesExpr.model_rebuild()
ReachableExpr.model_rebuild()
TracksExpr.model_rebuild()
RisksExpr.model_rebuild()
ChannelsExpr.model_rebuild()
RevisionsExpr.model_rebuild()
UnitsExpr.model_rebuild()
ArithExpr.model_rebuild()
CompareExpr.model_rebuild()
SetOpExpr.model_rebuild()
InExpr.model_rebuild()
NotExpr.model_rebuild()
AndExpr.model_rebuild()
OrExpr.model_rebuild()
ImpliesExpr.model_rebuild()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_SET_TYPES = {DSLType.RELATION_SET, DSLType.CHARM_SET, DSLType.SET_STR, DSLType.SET_INT}


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _peek(self) -> Token:
        return self._tokens[self._pos]

    def _advance(self) -> Token:
        token = self._tokens[self._pos]
        self._pos += 1
        return token

    def _expect(self, kind: TokenKind) -> Token:
        token = self._peek()
        if token.kind != kind:
            raise DSLSyntaxError(
                f"Expected {kind.value} at position {token.pos}, got {token.kind.value} ({token.value!r})"
            )
        return self._advance()

    def _match(self, *kinds: TokenKind) -> Token | None:
        if self._peek().kind in kinds:
            return self._advance()
        return None

    def _is_ident(self, value: str) -> bool:
        t = self._peek()
        return t.kind == TokenKind.IDENT and t.value == value

    def _is_next_ident(self, value: str) -> bool:
        """Check whether the token one position ahead has the given identifier value."""
        if self._pos + 1 >= len(self._tokens):
            return False
        t = self._tokens[self._pos + 1]
        return t.kind == TokenKind.IDENT and t.value == value

    # ------------------------------------------------------------------
    # Grammar productions
    # ------------------------------------------------------------------

    def parse(self) -> AnyExpr:
        node = self._implies_expr()
        self._expect(TokenKind.EOF)
        return node

    def _implies_expr(self) -> AnyExpr:
        # Right-associative: a => b => c  is  a => (b => c)
        left = self._or_expr()
        if self._match(TokenKind.IMPLIES):
            right = self._implies_expr()
            return ImpliesExpr(antecedent=left, consequent=right)
        return left

    def _or_expr(self) -> AnyExpr:
        left = self._and_expr()
        while self._is_ident("or"):
            self._advance()
            right = self._and_expr()
            left = OrExpr(left=left, right=right)
        return left

    def _and_expr(self) -> AnyExpr:
        left = self._not_expr()
        while self._is_ident("and"):
            self._advance()
            right = self._not_expr()
            left = AndExpr(left=left, right=right)
        return left

    def _not_expr(self) -> AnyExpr:
        if self._is_ident("not") and not self._is_next_ident("in"):
            self._advance()  # consume 'not'
            return NotExpr(arg=self._not_expr())
        return self._in_expr()

    def _in_expr(self) -> AnyExpr:
        left = self._compare_expr()
        if self._is_ident("not") and self._is_next_ident("in"):
            self._advance()  # consume 'not'
            self._advance()  # consume 'in'
            right = self._compare_expr()
            return InExpr(element=left, collection=right, negated=True)
        if self._is_ident("in"):
            self._advance()  # consume 'in'
            right = self._compare_expr()
            return InExpr(element=left, collection=right, negated=False)
        return left

    def _compare_expr(self) -> AnyExpr:
        left = self._set_op_expr()
        cmp_ops: dict[TokenKind, Literal["==", "!=", "<", "<=", ">", ">="]] = {
            TokenKind.EQ: "==",
            TokenKind.NEQ: "!=",
            TokenKind.LT: "<",
            TokenKind.LE: "<=",
            TokenKind.GT: ">",
            TokenKind.GE: ">=",
        }
        token = self._peek()
        if token.kind in cmp_ops:
            self._advance()
            right = self._set_op_expr()
            return CompareExpr(op=cmp_ops[token.kind], left=left, right=right)
        return left

    def _set_op_expr(self) -> AnyExpr:
        left = self._add_expr()
        set_ops = {
            TokenKind.PIPE: "|",
            TokenKind.AMP: "&",
            TokenKind.MINUS: "-",
        }
        while self._peek().kind in set_ops:
            op = set_ops[self._peek().kind]
            self._advance()
            right = self._add_expr()
            left = SetOpExpr(op=op, left=left, right=right)
        return left

    def _add_expr(self) -> AnyExpr:
        left = self._mul_expr()
        while self._peek().kind == TokenKind.PLUS:
            self._advance()
            right = self._mul_expr()
            left = ArithExpr(op="+", left=left, right=right)
        return left

    def _mul_expr(self) -> AnyExpr:
        left = self._atom()
        while self._peek().kind == TokenKind.STAR:
            self._advance()
            right = self._atom()
            left = ArithExpr(op="*", left=left, right=right)
        return left

    def _atom(self) -> AnyExpr:
        token = self._peek()

        # Parenthesised expression
        if token.kind == TokenKind.LPAREN:
            self._advance()
            node = self._implies_expr()
            self._expect(TokenKind.RPAREN)
            return node

        # Function calls and keywords
        if token.kind == TokenKind.IDENT:
            name = token.value

            # Call expressions: len(...), bool(...), charms(...), units(...) etc.
            if name in (
                "len",
                "bool",
                "charms",
                "features",
                "reachable",
                "tracks",
                "risks",
                "channels",
                "revisions",
                "units",
                "set",
            ):
                self._advance()
                self._expect(TokenKind.LPAREN)
                arg = self._implies_expr()
                self._expect(TokenKind.RPAREN)
                # set(config[key]) is a special case - produces SetConfigExpr, not a generic func
                if name == "set" and isinstance(arg, ConfigExpr):
                    return SetConfigExpr(key=arg.key)
                # set(resources[key]) is a special case - produces SetResourceExpr
                if name == "set" and isinstance(arg, ResourceExpr):
                    return SetResourceExpr(key=arg.key)
                match name:
                    case "len":
                        return LenExpr(arg=arg)
                    case "bool":
                        return BoolFunc(arg=arg)
                    case "charms":
                        return CharmsExpr(arg=arg)
                    case "features":
                        return FeaturesExpr(arg=arg)
                    case "reachable":
                        return ReachableExpr(arg=arg)
                    case "tracks":
                        return TracksExpr(arg=arg)
                    case "risks":
                        return RisksExpr(arg=arg)
                    case "channels":
                        return ChannelsExpr(arg=arg)
                    case "revisions":
                        return RevisionsExpr(arg=arg)
                    case "units":
                        return UnitsExpr(arg=arg)
                    case _:
                        # "set" reached here means set(non-config/non-resource) which is not supported.
                        raise DSLSyntaxError(
                            f"Unsupported use of '{name}(...)' at position {token.pos}; "
                            "set() only accepts config[key] or resource[key] arguments"
                        )

            # endpoint[name]
            if name == "endpoint":
                self._advance()
                self._expect(TokenKind.LBRACKET)
                name_token = self._expect(TokenKind.IDENT)
                self._expect(TokenKind.RBRACKET)
                return EndpointExpr(name=name_token.value)

            # config[key]
            if name == "config":
                self._advance()
                self._expect(TokenKind.LBRACKET)
                key_token = self._expect(TokenKind.IDENT)
                self._expect(TokenKind.RBRACKET)
                return ConfigExpr(key=key_token.value)

            # resources[key]
            if name == "resource":
                self._advance()
                self._expect(TokenKind.LBRACKET)
                key_token = self._expect(TokenKind.IDENT)
                self._expect(TokenKind.RBRACKET)
                return ResourceExpr(key=key_token.value)

            # juju_constraint[key]
            if name == "juju_constraint":
                self._advance()
                self._expect(TokenKind.LBRACKET)
                key_token = self._expect(TokenKind.IDENT)
                self._expect(TokenKind.RBRACKET)
                return JujuConstraintExpr(key=key_token.value)

            raise DSLSyntaxError(f"Unexpected identifier {name!r} at position {token.pos}")

        # Brace expressions: {self} or {v1, v2, ...}
        if token.kind == TokenKind.LBRACE:
            return self._brace_expr()

        # String literal
        if token.kind == TokenKind.STR:
            self._advance()
            return StrLit(value=token.value)

        # Integer literal
        if token.kind == TokenKind.INT:
            self._advance()
            return IntLit(value=int(token.value))

        raise DSLSyntaxError(f"Unexpected token {token.kind.value} ({token.value!r}) at position {token.pos}")

    def _brace_expr(self) -> AnyExpr:
        self._expect(TokenKind.LBRACE)

        # {self}
        if self._is_ident("self"):
            self._advance()
            self._expect(TokenKind.RBRACE)
            return SelfExpr()

        # {v1, v2, ...} — must be non-empty; homogeneous int or str
        elements: list[IntLit | StrLit] = []
        elements.append(self._literal())
        while self._match(TokenKind.COMMA):
            # Allow trailing comma before }
            if self._peek().kind == TokenKind.RBRACE:
                break
            elements.append(self._literal())
        self._expect(TokenKind.RBRACE)

        if isinstance(elements[0], IntLit):
            if not all(isinstance(e, IntLit) for e in elements):
                raise DSLSyntaxError("Literal set elements must all be the same type (int or str)")
            return IntLiteralSet(elements=frozenset(e.value for e in elements if isinstance(e, IntLit)))

        if not all(isinstance(e, StrLit) for e in elements):
            raise DSLSyntaxError("Literal set elements must all be the same type (int or str)")
        return StrLiteralSet(elements=frozenset(e.value for e in elements if isinstance(e, StrLit)))

    def _literal(self) -> IntLit | StrLit:
        token = self._peek()
        if token.kind == TokenKind.INT:
            self._advance()
            return IntLit(value=int(token.value))
        if token.kind == TokenKind.STR:
            self._advance()
            return StrLit(value=token.value)
        raise DSLSyntaxError(
            f"Expected integer or string literal at position {token.pos}, got {token.kind.value} ({token.value!r})"
        )


# ---------------------------------------------------------------------------
# Type checker
# ---------------------------------------------------------------------------


def _type_error(msg: str) -> DSLTypeError:
    return DSLTypeError(msg)


def _check_types(node: AnyExpr) -> AnyExpr:  # noqa: C901 (intentionally large switch)
    """Walk the AST and validate operand types.

    Returns the same node with SetOpExpr.dsl_type filled in, or raises
    DSLTypeError if any type rule is violated.
    """
    match node:
        # Leaves — always valid
        case (
            IntLit()
            | StrLit()
            | IntLiteralSet()
            | StrLiteralSet()
            | SelfExpr()
            | EndpointExpr()
            | ConfigExpr()
            | SetConfigExpr()
            | ResourceExpr()
            | SetResourceExpr()
        ):
            return node

        case LenExpr(arg=arg):
            arg = _check_types(arg)
            if arg.dsl_type not in (DSLType.RELATION_SET, DSLType.UNIT_SET):
                raise _type_error(f"len() requires a RelationSet or UnitSet argument, got {arg.dsl_type.value}")
            return LenExpr(arg=arg)

        case BoolFunc(arg=arg):
            arg = _check_types(arg)
            if arg.dsl_type != DSLType.RELATION_SET:
                raise _type_error(f"bool() requires RelationSet argument, got {arg.dsl_type.value}")
            return BoolFunc(arg=arg)

        case CharmsExpr(arg=arg):
            arg = _check_types(arg)
            if arg.dsl_type != DSLType.RELATION_SET:
                raise _type_error(f"charms() requires RelationSet argument, got {arg.dsl_type.value}")
            return CharmsExpr(arg=arg)

        case FeaturesExpr(arg=arg):
            arg = _check_types(arg)
            if arg.dsl_type != DSLType.RELATION_SET:
                raise _type_error(f"features() requires RelationSet argument, got {arg.dsl_type.value}")
            return FeaturesExpr(arg=arg)

        case ReachableExpr(arg=arg):
            arg = _check_types(arg)
            if not isinstance(arg, EndpointExpr):
                raise _type_error("reachable() argument must be endpoint[name] directly")
            return ReachableExpr(arg=arg)

        case TracksExpr(arg=arg):
            arg = _check_types(arg)
            if arg.dsl_type != DSLType.CHARM_SET:
                raise _type_error(f"tracks() requires CharmSet argument, got {arg.dsl_type.value}")
            return TracksExpr(arg=arg)

        case RisksExpr(arg=arg):
            arg = _check_types(arg)
            if arg.dsl_type != DSLType.CHARM_SET:
                raise _type_error(f"risks() requires CharmSet argument, got {arg.dsl_type.value}")
            return RisksExpr(arg=arg)

        case ChannelsExpr(arg=arg):
            arg = _check_types(arg)
            if arg.dsl_type != DSLType.CHARM_SET:
                raise _type_error(f"channels() requires CharmSet argument, got {arg.dsl_type.value}")
            return ChannelsExpr(arg=arg)

        case RevisionsExpr(arg=arg):
            arg = _check_types(arg)
            if arg.dsl_type != DSLType.CHARM_SET:
                raise _type_error(f"revisions() requires CharmSet argument, got {arg.dsl_type.value}")
            return RevisionsExpr(arg=arg)

        case UnitsExpr(arg=arg):
            arg = _check_types(arg)
            if arg.dsl_type != DSLType.CHARM_SET:
                raise _type_error(f"units() requires CharmSet argument, got {arg.dsl_type.value}")
            return UnitsExpr(arg=arg)

        case JujuConstraintExpr(key=key):
            valid = sorted(_JUJU_CONSTRAINT_KEYS)
            if key not in _JUJU_CONSTRAINT_KEYS:
                raise _type_error(f"Unknown juju_constraint key {key!r}. Valid keys: {valid}")
            return JujuConstraintExpr(key=key)

        case ArithExpr(op=op, left=left, right=right):
            left = _check_types(left)
            right = _check_types(right)
            if left.dsl_type != DSLType.INT:
                raise _type_error(f"Arithmetic operator '{op}' requires Int left operand, got {left.dsl_type.value}")
            if right.dsl_type != DSLType.INT:
                raise _type_error(f"Arithmetic operator '{op}' requires Int right operand, got {right.dsl_type.value}")
            return ArithExpr(op=op, left=left, right=right)

        case CompareExpr(op=op, left=left, right=right):
            left = _check_types(left)
            right = _check_types(right)
            lt = left.dsl_type
            rt = right.dsl_type
            # Strict numeric-only comparisons
            if op in ("<", ">"):
                if lt != DSLType.INT or rt != DSLType.INT:
                    raise _type_error(f"Operator '{op}' requires Int operands, got {lt.value} and {rt.value}")
            elif op in (">=", "<="):
                # Numeric comparison (Int) or set superset/subset check
                is_numeric = lt == DSLType.INT and rt == DSLType.INT
                is_set = lt in _SET_TYPES and rt in _SET_TYPES
                if is_set and lt != rt:
                    raise _type_error(f"Set operator '{op}' requires matching set types, got {lt.value} and {rt.value}")
                if not is_numeric and not is_set:
                    raise _type_error(
                        f"Operator '{op}' requires Int or matching set operands, got {lt.value} and {rt.value}"
                    )
            else:
                # == and != require matching types (UNRESOLVED config is allowed to match any)
                if lt != rt and lt != DSLType.RUNTIME and rt != DSLType.RUNTIME:
                    raise _type_error(f"Operator '{op}' requires matching types, got {lt.value} and {rt.value}")
                if lt == DSLType.UNIT_SET or rt == DSLType.UNIT_SET:
                    raise _type_error(
                        f"UnitSet cannot be used in '{op}' comparisons; use len(units(...)) to compare unit counts"
                    )
            return CompareExpr(op=op, left=left, right=right)

        case SetOpExpr(op=op, left=left, right=right):
            left = _check_types(left)
            right = _check_types(right)
            lt = left.dsl_type
            rt = right.dsl_type

            # Arithmetic subtraction: both INT
            if lt == DSLType.INT and rt == DSLType.INT:
                return ArithExpr(op="-", left=left, right=right)

            # Set operation: both must be the same set type
            if lt not in _SET_TYPES or rt not in _SET_TYPES:
                raise _type_error(
                    f"Operator '{op}' requires two set operands or two Int operands, " f"got {lt.value} and {rt.value}"
                )
            if lt != rt:
                raise _type_error(f"Set operator '{op}' requires matching set types, got {lt.value} and {rt.value}")
            return SetOpExpr(op=op, left=left, right=right, dsl_type=lt)

        case InExpr(element=element, collection=collection, negated=negated):
            element = _check_types(element)
            collection = _check_types(collection)
            et = element.dsl_type
            ct = collection.dsl_type

            # Substring containment: both Str
            if et == DSLType.STR and ct == DSLType.STR:
                return InExpr(element=element, collection=collection, negated=negated)

            # Literal set or runtime set membership
            if ct == DSLType.SET_INT:
                if et not in (DSLType.INT, DSLType.RUNTIME):
                    raise _type_error(f"'in Set[Int]' requires Int element, got {et.value}")
                return InExpr(element=element, collection=collection, negated=negated)

            if ct == DSLType.SET_STR:
                if et not in (DSLType.STR, DSLType.RUNTIME):
                    raise _type_error(f"'in Set[Str]' requires Str element, got {et.value}")
                return InExpr(element=element, collection=collection, negated=negated)

            raise _type_error(f"'in' operator requires a set or Str collection, got {ct.value}")

        case NotExpr(arg=arg):
            arg = _check_types(arg)
            if arg.dsl_type not in (DSLType.BOOL, DSLType.RUNTIME):
                raise _type_error(f"'not' requires Bool operand, got {arg.dsl_type.value}")
            return NotExpr(arg=arg)

        case AndExpr(left=left, right=right):
            left = _check_types(left)
            right = _check_types(right)
            if left.dsl_type != DSLType.BOOL:
                raise _type_error(f"'and' requires Bool left operand, got {left.dsl_type.value}")
            if right.dsl_type != DSLType.BOOL:
                raise _type_error(f"'and' requires Bool right operand, got {right.dsl_type.value}")
            return AndExpr(left=left, right=right)

        case OrExpr(left=left, right=right):
            left = _check_types(left)
            right = _check_types(right)
            if left.dsl_type != DSLType.BOOL:
                raise _type_error(f"'or' requires Bool left operand, got {left.dsl_type.value}")
            if right.dsl_type != DSLType.BOOL:
                raise _type_error(f"'or' requires Bool right operand, got {right.dsl_type.value}")
            return OrExpr(left=left, right=right)

        case ImpliesExpr(antecedent=antecedent, consequent=consequent):
            antecedent = _check_types(antecedent)
            consequent = _check_types(consequent)
            if antecedent.dsl_type != DSLType.BOOL:
                raise _type_error(f"'=>' requires Bool antecedent, got {antecedent.dsl_type.value}")
            if consequent.dsl_type != DSLType.BOOL:
                raise _type_error(f"'=>' requires Bool consequent, got {consequent.dsl_type.value}")
            return ImpliesExpr(antecedent=antecedent, consequent=consequent)

    raise DSLSyntaxError(f"Unknown node type: {type(node)}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_constraint(text: str) -> AnyExpr:
    """Parse a single DSL constraint string into a typed AST.

    Raises:
        DSLSyntaxError: if the input cannot be tokenized or parsed.
        DSLTypeError: if an operator receives operands of incompatible types.
    """
    tokens = _tokenize(text)
    parser = _Parser(tokens)
    tree = parser.parse()
    try:
        return _check_types(tree)
    except DSLTypeError as exc:
        raise DSLTypeError(f"{exc} (in constraint: {text!r})") from exc
