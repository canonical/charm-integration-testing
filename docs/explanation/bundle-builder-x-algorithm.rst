The bundle builder X algorithm
==============================

Bundle builder X uses an SMT solver (`Z3 <https://github.com/Z3Prover/z3>`_) to resolve charm
compatibility constraints, replacing the earlier graph-traversal approach.
Instead of exploring a search tree of possible bundles, the solver encodes the entire
problem as a set of logical constraints and finds a satisfying assignment directly.

Why a solver?
-------------

An SMT solver is well suited to this problem because:

- The search space grows combinatorially with the number of charms and endpoints;
  graph traversal approaches do not scale.
- Constraint types like "at most one of these three endpoints" or "the charm on
  the other end of this endpoint must share the same channel track" are awkward to
  express as graph edge weights but natural to encode as logical assertions.
- Multi-model specs with cross-model relations add another dimension that a
  solver handles in the same pass.

Constraints are first-class: you declare what must hold and the solver finds a
valid assignment, or proves none exists.

How it works
------------

The build loop has three phases that repeat until the problem is satisfiable:

1. **Domain construction** -- The spec file names applications and their charms.
   Charm metadata is fetched from Charmhub (with local overrides applied on top).
   Each charm, its endpoints, and its config options become part of the "domain",
   the set of Z3 variables and their possible values.

2. **Constraint generation** -- Constraints are added to the solver for every
   rule the bundle must satisfy:

   - Each application must map to exactly one charm.
   - Platform and architecture must match.
   - Local integrations connect two endpoints in the same model with a compatible
     interface.
   - Cross-model integrations connect endpoints across models via offers.
   - Non-optional endpoints must be integrated.
   - Per-charm override constraints (the :doc:`/reference/constraint-dsl`)
     express things like mutual exclusion, feature negotiation, track matching,
     certificate proxy chains, and config requirements.

3. **Solve and expand** -- The solver checks satisfiability. If the problem is
   ``unsat``, the unsat core (the minimal set of conflicting constraints) is
   decoded to determine what went wrong. The builder then lazily expands the
   domain -- creating **one new integration variable** (reusing an in-domain
   charm where possible, otherwise instantiating one new charm) per unsatisfied
   endpoint per iteration -- and loops back to step 2. If the problem is
   ``sat``, an optimization pass minimizes the number of applications and
   integrations, and the result is extracted into concrete ``Bundle`` objects.

.. mermaid::

   flowchart TD
       A[Parse spec file] --> B[Build initial domain]
       B --> C[Generate constraints]
       C --> D{Satisfiable?}
       D -- yes --> E[Optimize & extract solution]
       D -- no --> F[Decode unsat core]
       F --> G[Expand domain]
       G --> C

Domain expansion
~~~~~~~~~~~~~~~~

When the solver returns ``unsat``, the builder decodes every tag in the unsat
core and attempts to expand the domain for each one in the same iteration.
Tags are processed in priority order, but all tags are acted on (not just the
first):

- ``APPLICATION_EXISTS`` -- the spec references an application whose charm is
  not yet in the domain. Fetch it from Charmhub.
- ``APPLICATION_INTEGRATION_EXISTS`` -- an explicit integration references
  applications not yet in the domain. Fetch them, then create the integration
  variable between the two named endpoints.
- ``CHARM_ENDPOINT_NON_OPTIONAL`` -- a charm has a non-optional endpoint that
  is not yet connected. The builder creates **exactly one new integration
  variable** to satisfy it (see *Lazy integration materialization* below).
- ``ENDPOINT_COUNT_MATCHES_INTEGRATIONS`` / ``PEER_CHANNEL_MISMATCH`` --
  structural mismatches that indicate a constraint conflict.

Pairs of ``PEER_CHANNEL_MISMATCH`` tags for the same (anchor, peer) charm
pair are merged before processing so that track and risk constraints are
resolved together in a single step, rather than one dimension at a time.

This loop runs until one of three outcomes occurs: the problem becomes ``sat``,
the unsat core contains no tag the builder can act on (the domain cannot be
expanded further and the problem is provably unsatisfiable, so the builder
raises ``UncompletableBundleError``), or the per-iteration SAT check exceeds
its configurable timeout (default 1 minute), in which case ``UncompletableBundleError``
is raised immediately rather than hanging indefinitely.  There is no fixed
iteration cap; arbitrarily deep dependency graphs converge naturally without an
artificial limit.

Lazy integration materialization
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Integration variables (one Z3 boolean per possible relation between two charm
endpoints) are **not** created eagerly. A naive "all pairs" approach -- creating
a variable for every compatible (provider, requirer) endpoint pair as charms are
added -- produces ``O(providers x requirers)`` variables per interface. When many
charms share an interface, this dominates both constraint-building and solve time,
and identical interchangeable providers create a factorial number of symmetric
solutions that the solver must search.

Instead, integration variables are materialized lazily by the solve loop, the
same way charms are. When an endpoint demands a connection, the builder applies a
capacity-aware, reuse-before-instantiate rule:

1. **Reuse** -- connect to an existing in-domain charm whose compatible endpoint
   has spare capacity, creating a single integration variable and no new charm.
2. **Instantiate** -- if no reusable partner exists, fetch one new charm from
   Charmhub (highest priority first) and wire to it. This includes a fresh
   instance of a charm whose existing instances are all saturated -- for example
   a second ``limit: 1`` provider for a second consumer.

An endpoint is *saturated* when it already has as many integration variables as
its declared ``limit`` allows; offering another variable to a saturated endpoint
cannot help (the solver would have to drop an existing consumer, which simply
re-triggers expansion), so the builder instantiates a fresh partner instead.

This keeps the integration-variable count proportional to actual endpoint demand
rather than to the product of providers and requirers:

- an **unlimited** provider is shared by every consumer (one variable each);
- a ``limit: N`` provider saturates after ``N`` consumers, so the next consumer
  instantiates a fresh instance -- ``ceil(demand / N)`` instances in total.

On real ``opentelemetry-collector-k8s`` specs this produces 3.6x-16x fewer
integration variables than the all-pairs scheme on the same final domain, and it
eliminates the symmetric-provider blow-up that previously caused multi-hour
solves. Completeness is preserved: variables are only ever added, never removed,
so any feasible solution remains reachable; and offering the highest-priority
partner first makes the first satisfying assignment use the lowest-cost option.

Key properties
--------------

- **Explicit constraint DSL** -- constraints are compiled to Z3 assertions rather
  than embedded implicitly in scoring or edge expansion.
- **Multi-model** -- all models in a spec are solved simultaneously.
- **Cross-model relations** -- both in-spec and external CMRs are supported.
- **Optimization pass** -- after a satisfying assignment is found, the builder
  first attempts a ``z3.Optimize`` pass (configurable timeout, default 1 minute)
  to find a globally optimal solution in one shot. If that times out, it falls
  back to iterative descent: a ``z3.Solver`` loop that minimizes charm cost first
  (phase 1), then integration count with charm cost fixed (phase 2), each step
  issuing a single SAT query with a tighter bound.
- **Failure diagnostics** -- when the problem is unsatisfiable, the unsat core is
  decoded into specific constraint tags so callers know exactly what went wrong.
