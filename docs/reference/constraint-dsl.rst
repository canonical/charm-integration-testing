Constraint DSL reference
========================

Overview
--------

The constraint DSL is a simple expression language for writing per-charm integration
constraints in override YAML files. Constraints are evaluated against a proposed bundle
solution and must hold for the bundle to be valid.

Constraints are written as a list of boolean expressions under the ``constraints:`` key:

.. code-block:: yaml

   constraints:
     - len(endpoint[database]) + len(endpoint[db]) <= 1
     - bool(endpoint[vault-pki]) => bool(endpoint[tls-certificates-pki])
     - 'config[num-history-shards] in {1, 2, 4, 8, 16}'

Each expression must evaluate to a boolean. All constraints in the list must hold
simultaneously.

.. note::

   **YAML quoting**: Single-quote any constraint that contains YAML-special characters.
   The most common triggers are ``{...}`` set literals (parsed as flow mappings) and ``#``
   (parsed as a comment). Single quotes are the safest default:
   ``- 'features(endpoint[admin]) == {"admin"}'``
   ``- 'config[num-history-shards] in {1, 2, 4, 8}'``

Z3 encoding
-----------

All DSL expressions lower to **boolean, integer, or finite-domain string constraints**.
The key invariant is that no **unbounded** string variables are introduced; every
string variable in the solver has a finite, known set of possible values.

- ``charms()`` lowers to one ``z3.Bool`` per directly wired charm in the domain.
  ``reachable()`` extends this by also including charms reachable via proxy
  declarations, using the same ``z3.Bool`` encoding over a finite domain.
- ``features()`` lowers to one ``z3.Bool`` per feature in the domain. Features are
  only meaningful on physical relations; ``features()`` accepts a ``RelationSet`` and
  does not compose with ``reachable()``.
- ``risks()``, ``tracks()``, ``channels()`` operate over finite enumerated domains known
  at constraint-generation time (e.g. risk is always one of ``stable``, ``candidate``,
  ``beta``, ``edge``). These can be encoded as Z3 string constants over a closed domain.
- ``config[key]`` for string-typed configs is encoded as a Z3 string variable
  constrained to the finite set of values declared in overrides. Unbounded string
  config variables are not supported.
- ``resource[key]`` is encoded as a Z3 string variable constrained to the finite set
  of values declared in the charm's ``resources:`` overrides. As with configs, only
  override-declared resource keys may appear in constraints.
- String literals in constraints (e.g. ``{"stable"}``, ``{"broker"}``) are always
  matched against a closed, finite domain.

Z3 handles equality and membership constraints over finite string domains efficiently.
The concern with string theory is open-ended reasoning (regex, concatenation, arbitrary
length), none of which appears here.

Types
-----

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Type
     - Description
   * - ``Int``
     - Integer value
   * - ``Bool``
     - Boolean value
   * - ``RelationSet``
     - Set of active relation IDs on an endpoint
   * - ``CharmSet``
     - Set of charm instance IDs
   * - ``Set[Str]``
     - Set of strings
   * - ``Set[Int]``
     - Set of integers

Type rules
~~~~~~~~~~

The DSL is statically typed. Applying an operator to operands of incompatible types
is a parse-time error, not a silent Z3 failure.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Operator
     - Required operand types
   * - ``+``, ``-``, ``*``
     - Both ``Int``
   * - ``<``, ``<=``, ``>``, ``>=``
     - Both ``Int``
   * - ``==``, ``!=``
     - Both operands must be the same type (``Int == Int``, ``Bool == Bool``, ``Set[Str] == Set[Str]``, etc.)
   * - ``|``, ``&``, ``-`` (set)
     - Both operands must be the same set type
   * - ``>=``, ``<=`` (set)
     - Both operands must be the same set type
   * - ``x in S`` (set membership)
     - ``x`` must match the element type of ``S`` (``Str in Set[Str]``, ``Int in Set[Int]``)
   * - ``x in S`` (substring)
     - Both ``Str``
   * - ``x in {v1, v2, ...}`` (literal set)
     - ``x`` and every ``vi`` must be the same scalar type
   * - ``reachable()``
     - Argument must be ``endpoint[x]`` directly; returns ``CharmSet``
   * - ``features()``
     - Argument must be ``RelationSet`` (``endpoint[x]`` or ``RelationSet`` expression); does not accept ``CharmSet``
   * - ``not``
     - ``Bool``
   * - ``and``, ``or``, ``=>``
     - Both ``Bool``

Examples of type errors caught at parse time::

   len(endpoint[x]) == {"stable"}    # error: Int == Set[Str]
   risks({self}) == len(endpoint[x]) # error: Set[Str] == Int
   bool(endpoint[x]) + 1             # error: Bool + Int

Primitives
----------

Endpoint relation set
~~~~~~~~~~~~~~~~~~~~~

::

   endpoint[<name>]  ->  RelationSet

The set of **direct Juju wires** on the named endpoint. ``endpoint[x]`` always refers
to physical relations only; no proxy-resolved entries are included. The aggregation
functions ``len``, ``bool``, ``charms``, and ``features`` all accept a ``RelationSet``.

To include charms reachable via proxy declarations, use ``reachable(endpoint[x])``
instead.

Endpoint count
~~~~~~~~~~~~~~

::

   len(endpoint[<name>])  ->  Int
   len(RelationSet)       ->  Int

The number of direct Juju wires on the named endpoint. The ``endpoint_{name}_count``
solver variable tracks this count.

Endpoint boolean
~~~~~~~~~~~~~~~~

::

   bool(endpoint[<name>])  ->  Bool
   bool(RelationSet)       ->  Bool

True if the endpoint has at least one active integration. Sugar for
``len(endpoint[<name>]) >= 1``. Maps to the ``endpoint_{name}_integrated`` boolean
variable in the solver. Use this in boolean contexts instead of relying on implicit
coercion.

Endpoint charm set
~~~~~~~~~~~~~~~~~~

::

   charms(endpoint[<name>])  ->  CharmSet
   charms(RelationSet)       ->  CharmSet

The set of charm instance IDs **directly wired** to the named endpoint. Only
physical Juju relations are included; proxy declarations are not walked.

To include charms reachable via proxy chains, use ``reachable(endpoint[x])``.

Reachable charm set
~~~~~~~~~~~~~~~~~~~

::

   reachable(endpoint[<name>])  ->  CharmSet

The set of charm instance IDs reachable from the named endpoint, including both
**directly wired** charms and any charms whose proxy declarations are reachable via
the proxy chain anchored to a physical wire.

Proxy resolution walks outward from each physical wire: for each charm already in
the set, any other charm that has a proxy declaration for this endpoint's interface
(with its ``via`` endpoint wired to a charm already in the set) is added. No proxy
entries are added if the endpoint has no physical wires at all.

For example, in the juju-jimm scenario: ssc is directly wired to ``receive-ca-cert``
(physical); traefik declares ``via: certificates`` which is wired to ssc, so traefik
is added; hydra declares ``via: public-ingress`` which is wired to traefik, so hydra
is added. ``reachable(endpoint[receive-ca-cert])`` = {ssc, traefik, hydra}.

Contrast with ``charms(endpoint[receive-ca-cert])`` = {ssc} (directly wired only).

.. list-table::
   :header-rows: 1
   :widths: 35 15 50

   * - Expression
     - Returns
     - Includes proxy chain?
   * - ``charms(endpoint[x])``
     - ``CharmSet``
     - No, directly wired only
   * - ``reachable(endpoint[x])``
     - ``CharmSet``
     - Yes, physical + proxy-reachable

Endpoint feature set
~~~~~~~~~~~~~~~~~~~~

::

   features(endpoint[<name>])  ->  Set[Str]
   features(RelationSet)       ->  Set[Str]

The set of distinct features active across all relations on the named endpoint.
Features must be declared in the endpoint's ``features:`` list in overrides; this
closed-world declaration is what allows features to be encoded as ``z3.Bool`` variables
rather than Z3 string variables.

The ``features:`` declaration also automatically generates a structural constraint:
``endpoint_integrated => feature_x`` for each declared feature. The DSL ``features()``
function is used to further constrain *which* features must or must not be active:

.. code-block:: yaml

   requires:
     admin:
       optional: false
       features: ["admin"]   # declares the feature universe; generates integrated => feature_admin
   constraints:
     - bool(endpoint[admin]) => features(endpoint[admin]) == {"admin"}  # pins to exactly this feature set

The two mechanisms have distinct roles:

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Mechanism
     - Role
   * - ``features: [...]`` on endpoint
     - Declares feature universe; enables boolean encoding; generates ``integrated => feature_x``
   * - ``features(endpoint[x]) == {...}`` in DSL
     - Restricts which features may be active; prevents undeclared combinations

Feature coherence across an integration
'''''''''''''''''''''''''''''''''''''''

When two endpoints are integrated, a feature declared on only one side is
forced inactive, and a feature declared on both sides must agree. This
prevents pairing charms whose feature sets don't overlap. Failures are
reported via ``INTEGRATION_FEATURE_MISMATCH`` and
``FeatureMismatchDiagnostic`` values in ``UncompletableBundleError.diagnostics``.

Config value
~~~~~~~~~~~~

::

   config[<key>]  ->  Int | Bool | Str

The value the solver will assign to the named config option. Supports ``int``,
``boolean``, and ``string`` config types. String config variables are constrained to
the finite set of values declared in the charm's ``configs:`` overrides; unbounded
string configs are not supported.

Resource value
~~~~~~~~~~~~~~

::

   resource[<key>]  ->  Str

The OCI image URL or file path the solver will assign to the named resource.
Resource variables are always strings, constrained to the finite set of values
declared in the charm's ``resources:`` overrides.

Operators
---------

Arithmetic
~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Expression
     - Result
     - Description
   * - ``a + b``
     - ``Int``
     - Addition
   * - ``a - b``
     - ``Int``
     - Subtraction
   * - ``a * b``
     - ``Int``
     - Multiplication

Integer comparison
~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Expression
     - Description
   * - ``a == b``
     - Equal
   * - ``a != b``
     - Not equal
   * - ``a < b``
     - Less than
   * - ``a <= b``
     - Less than or equal
   * - ``a > b``
     - Greater than
   * - ``a >= b``
     - Greater than or equal

Membership and containment
~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``in`` operator is overloaded across three forms. All three compile to Z3
constraints over finite domains.

**Literal set membership** -- true if the scalar equals one of the listed values.
Sugar for ``a == v1 or a == v2 or ...``. Maps to a Z3 ``Or`` of equality constraints.

::

   <Int> in {<v1>, <v2>, ...}  ->  Bool
   <Str> in {<v1>, <v2>, ...}  ->  Bool

**Runtime set membership** -- true if the value is an element of a set-typed
expression. See also the set comparison operators below.

::

   <Str> in Set[Str]  ->  Bool
   <Int> in Set[Int]  ->  Bool

**Substring containment** -- true if the left string contains the right string as a
substring. Intended for comma-separated string configs where the value cannot be
constrained with ``==`` alone:

::

   <Str> in <Str>  ->  Bool

.. code-block:: yaml

   - bool(endpoint[peer-cluster]) => "broker" in config[roles]

This form uses Z3 string theory and is safe only when the config variable is
constrained to a finite declared set of values. Do not use it with unbounded string
variables.

**Negation** -- ``not in`` is supported for all three forms and is sugar for
``not (x in ...)``:

::

   <value> not in {<v1>, <v2>, ...}  ->  Bool
   <value> not in <Set>              ->  Bool
   <Str>   not in <Str>              ->  Bool

Parentheses
~~~~~~~~~~~

Subexpressions may be grouped with parentheses to control precedence::

   (a or b) and (c or d)
   not (bool(endpoint[x]) and bool(endpoint[y]))
   (len(endpoint[a]) + len(endpoint[b])) == 1

Set operations
~~~~~~~~~~~~~~

All set types (``CharmSet``, ``Set[Str]``, ``Set[Int]``, ``RelationSet``) support the
following operations:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Expression
     - Description
   * - ``A | B``
     - Union: elements in A or B or both
   * - ``A & B``
     - Intersection: elements in both A and B
   * - ``A - B``
     - Subtraction: elements in A but not in B

Set comparison
~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Expression
     - Description
   * - ``A == B``
     - Sets contain exactly the same elements
   * - ``A != B``
     - Sets differ in at least one element
   * - ``A >= B``
     - A is a superset of B (every element of B is also in A)
   * - ``A <= B``
     - A is a subset of B (every element of A is also in B)
   * - ``x in A``
     - Element x is a member of set A
   * - ``x not in A``
     - Element x is not a member of set A

CharmSet accessors
~~~~~~~~~~~~~~~~~~

These return the **set of distinct values** for the given property across all charm instances in a
``CharmSet``. Operating on sets of distinct values makes quantification explicit:
``risks(A) == risks(B)`` means every charm instance in A and every charm instance in B share exactly one
common risk value.

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Expression
     - Returns
     - Description
   * - ``tracks(CharmSet)``
     - ``Set[Str]``
     - Distinct track values across all charms
   * - ``risks(CharmSet)``
     - ``Set[Str]``
     - Distinct risk values across all charms
   * - ``channels(CharmSet)``
     - ``Set[Str]``
     - Distinct full channel strings across all charms
   * - ``revisions(CharmSet)``
     - ``Set[Int]``
     - Distinct revision numbers across all charms

Self reference
~~~~~~~~~~~~~~

``{self}`` is the only ``CharmSet`` literal. It contains only the charm instance being
constrained. There is no syntax for constructing a ``CharmSet`` from named charm
identifiers; ``CharmSet`` values are only obtained via ``charms()`` or ``{self}``.

::

   risks({self})     ->  Set[Str]   # singleton set containing this charm's risk
   channels({self})  ->  Set[Str]   # singleton set containing this charm's channel
   tracks({self})    ->  Set[Str]   # singleton set containing this charm's track
   revisions({self}) ->  Set[Int]   # singleton set containing this charm's revision

Unit count
~~~~~~~~~~

::

   units(charm_set)        ->  UnitSet
   len(units(charm_set))   ->  Int

``units(charm_set)`` returns the set of deployed units for each charm in
``charm_set``.  Wrapping it with ``len()`` gives the total unit count as an
integer — matching the familiar ``len(endpoint[x])`` pattern for integration counts.

The most common form uses ``{self}`` (the current application):

.. code-block:: yaml

   constraints:
     - len(units({self})) >= 3

The solver minimises unit counts subject to constraints, so
``len(units({self})) >= 3`` produces exactly 3 units unless a higher value is
required by another constraint.

.. note::

   ``len(units({self}))`` returns ``Int`` and participates in the same
   arithmetic and comparison operators as ``len(endpoint[x])``.  For example:

   .. code-block:: yaml

      # Exactly 3 units
      - len(units({self})) == 3

      # Unit count must equal the number of active replication peers
      - len(units({self})) == len(endpoint[replication]) + 1

Logical
~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Expression
     - Description
   * - ``a and b``
     - Logical AND
   * - ``a or b``
     - Logical OR
   * - ``not a``
     - Logical NOT
   * - ``a => b``
     - Implication: if a then b (sugar for ``not a or b``)

Examples by constraint type
---------------------------

Mutual exclusion (constraint type 3)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Exactly one of two database endpoints must be active:

.. code-block:: yaml

   - len(endpoint[database]) + len(endpoint[database-legacy]) == 1

At most one of three mutually exclusive endpoints:

.. code-block:: yaml

   - len(endpoint[database]) + len(endpoint[db]) + len(endpoint[db-admin]) <= 1

.. note::

   ``len`` always counts direct Juju wires only. Proxy declarations do not
   affect cardinality constraints.

Conditional requirement (constraint type 4)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An endpoint becomes required when another is integrated:

.. code-block:: yaml

   # If vault-pki is integrated, a parent CA is required
   - bool(endpoint[vault-pki]) => bool(endpoint[tls-certificates-pki])

Same application (constraint type 7)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two endpoints must be integrated with the same charm:

.. code-block:: yaml

   # ldap and ldap-certificate-transfer must point to the same provider
   - charms(endpoint[ldap]) == charms(endpoint[ldap-certificate-transfer])

Minimum observability (constraint type 9)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

At least one of several optional endpoints must be integrated:

.. code-block:: yaml

   - len(endpoint[metrics-endpoint]) + len(endpoint[logging-provider]) + len(endpoint[tracing-provider]) + len(endpoint[grafana-dashboards-consumer]) >= 1

Minimum cardinality (constraint type 10)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An endpoint requires more than one integration:

.. code-block:: yaml

   # Backend requires at least 2 integrations for HA
   - len(endpoint[backend]) >= 2

Minimum unit count
~~~~~~~~~~~~~~~~~~

An application requires a minimum number of deployed units:

.. code-block:: yaml

   constraints:
     - len(units({self})) >= 3

The unit count may also be constrained relative to other integer expressions:

.. code-block:: yaml

   # Each replication peer must be matched by a local unit (plus one coordinator)
   - len(units({self})) == len(endpoint[replication]) + 1

Capability requirements (constraint type 5)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``admin`` endpoint must integrate with a provider that advertises exactly the
``admin`` feature (temporal-admin-k8s), not the ``ui`` feature (temporal-ui-k8s):

.. code-block:: yaml

   requires:
     admin:
       optional: false
       features: ["admin", "ui"]  # declare the full feature universe across both endpoints
     ui:
       optional: true
       features: ["admin", "ui"]
   constraints:
     - bool(endpoint[admin]) => features(endpoint[admin]) == {"admin"}
     - bool(endpoint[ui]) => features(endpoint[ui]) == {"ui"}

The ``features:`` declaration on each endpoint defines the closed universe of possible
feature values. The DSL constraints then pin each endpoint to exactly its required
feature set.

Config membership
~~~~~~~~~~~~~~~~~

The solver must pick a value from the allowed set:

.. code-block:: yaml

   - 'config[num-history-shards] in {1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024}'

Version compatibility (constraint type 8)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

All charms integrated on the replication endpoint must share the same risk as this charm:

.. code-block:: yaml

   - risks(charms(endpoint[replication])) == risks({self})

For a charm that requires its backend to be on the same full channel:

.. code-block:: yaml

   - bool(endpoint[backend-database]) => channels(charms(endpoint[backend-database])) == channels({self})

Using set subtraction to allow a grace window on risk (e.g. stable or candidate):

.. code-block:: yaml

   - risks(charms(endpoint[replication])) - {"candidate"} == risks({self}) - {"candidate"}

The set equality ``== risks({self})`` means "there is exactly one distinct risk value
across all integrated charms, and it equals mine." If the endpoint is not integrated
the left-hand set is empty and the equality fails, which is why the implication form
is used when the integration is optional.

Transitive capability (constraint type 11)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A charm may require that every charm reachable via the proxy chain on one endpoint
must also be reachable on a separate endpoint.

**Scenario**: juju-jimm is directly wired to self-signed-certificates on
``receive-ca-cert``. Traefik proxies ``certificates`` via its own ``certificates``
endpoint (wired to ssc). Hydra proxies ``certificates`` via its ``public-ingress``
endpoint (wired to traefik). juju-jimm also integrates with hydra on ``oauth``.
The constraint is: if the oauth integration has the ``tls`` feature, every charm
reachable via ``receive-ca-cert`` must include every charm on ``oauth``:

.. code-block:: yaml

   # juju-jimm overrides
   constraints:
     - '"tls" in features(endpoint[oauth]) => reachable(endpoint[receive-ca-cert]) >= charms(endpoint[oauth])'

``charms(endpoint[oauth])`` = {hydra} (directly wired). ``reachable(endpoint[receive-ca-cert])``
= {ssc, traefik, hydra}. The superset check passes: hydra is reachable via its own
proxy declaration, anchored through the ssc physical wire.

Proxy declarations are charm-level. Each entry says "I proxy interface X; my
implementation is whoever is on my ``via`` endpoint":

.. code-block:: yaml

   # hydra overrides
   proxies:
     - interface: certificates
       via: public-ingress  # hydra's requires endpoint, wired to traefik

   # traefik-k8s overrides
   proxies:
     - interface: certificates
       via: certificates    # traefik requires endpoint, wired to ssc

Proxy resolution walks the chain from the physical wire outward: ssc is in the
set (physical); the ``via`` for traefik is wired to ssc so traefik is added; the ``via``
for hydra is wired to traefik which is now in the set, so hydra is added.

To assert that only a directly wired connection is acceptable (no proxy chain):

.. code-block:: yaml

   - reachable(endpoint[certificates]) == charms(endpoint[certificates])

What is handled structurally (not in the DSL)
----------------------------------------------

Some constraint types map directly to YAML fields and do not require DSL expressions.
The solver enforces these automatically:

.. list-table::
   :header-rows: 1
   :widths: 25 20 55

   * - Constraint type
     - YAML field
     - Notes
   * - Optional / required endpoint
     - ``optional: true/false``
     - Endpoint must have >= 1 integration if ``false``
   * - Integration limit
     - ``limit: N``
     - Endpoint count <= N
   * - Resource value
     - ``resources: {name: [value]}``
     - Single-value resource override is always emitted; no DSL expression needed
   * - Capability feature requirement
     - ``features: [...]``
     - Generates ``integrated => feature_x`` automatically
   * - Acyclic integration graph
     - Structural
     - Enforced globally via topological rank constraints
   * - Minimum unit count (default)
     - Structural
     - Every application gets ``num_units >= 1`` automatically; raise the floor with ``len(units({self})) >= N``
