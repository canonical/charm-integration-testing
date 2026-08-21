The bundle builder X algorithm
==============================

Bundle builder X uses an SMT solver (`Z3 <https://github.com/Z3Prover/z3>`_) to resolve charm
compatibility constraints, replacing the earlier graph-traversal approach.
The builder encodes the currently known domain as logical constraints, then expands that
domain incrementally from solver diagnostics until it finds a satisfying assignment.

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

1. **Domain construction** -- The spec file defines models, applications, and
   integrations. These declarations become the initial domain. Charm metadata is
   fetched from Charmhub (with local overrides applied on top) as the domain expands.
   Each introduced charm, its endpoints, and its config options add Z3 variables and
   possible values to the domain.

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
   decoded to determine what went wrong. The builder then expands the domain,
   typically by fetching a new charm that can fulfill an unsatisfied endpoint,
   and loops back to step 2. If the problem is ``sat``, the builder exposes a
   bounded set of quality-improving alternatives, runs an optimization pass, and
   extracts the result into concrete ``Bundle`` objects.

.. mermaid::

   flowchart TD
       A[Parse spec file] --> B[Build initial domain]
       B --> C[Generate constraints]
       C --> D{Satisfiable?}
       D -- yes --> E[Prepare bounded optimization domain]
       E --> H[Optimize & extract solution]
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
  applications not yet in the domain. Fetch them.
- ``CHARM_ENDPOINT_NON_OPTIONAL`` -- a charm has a non-optional endpoint
  with no compatible charm in the domain. Reuse an existing compatible charm
  where possible; otherwise search Charmhub for a matching charm.
- ``ENDPOINT_COUNT_MATCHES_INTEGRATIONS`` -- an endpoint needs another
  integration. It uses the same reuse-and-search process.
- ``PEER_CHANNEL_MISMATCH`` / ``SUBORDINATE_BASE_MISMATCH`` -- fetch compatible
  channel or base variants and pair them with the actual counterpart.
- ``INTEGRATION_FEATURE_MISMATCH`` -- two integrated endpoints declared
  incompatible ``features:`` tags (see :doc:`/reference/constraint-dsl`); not
  expandable, so it remains in the unsat core.

Candidate expansion is deliberately asymmetric:

- For a user-requested application, all compatible direct candidates are exposed
  so optimization can compare the immediate alternatives.
- For a transitive dependency, only the first viable candidate is added. This
  prevents every common interface from recursively adding its entire
  dependency closure.

Candidates are sorted by charm priority and then name. Each new candidate is
initially paired only with its parent charm; it is not eagerly paired against
the whole domain.

Pairs of ``PEER_CHANNEL_MISMATCH`` tags for the same (anchor, peer) charm
pair are merged before processing so that track and risk constraints are
resolved together in a single step, rather than one dimension at a time.

There is no hard iteration limit: valid dependency graphs may require an
arbitrary number of expansion steps. Each solver call has a timeout. If the
domain cannot be expanded further and the problem is still unsatisfiable, the
builder raises ``UncompletableBundleError`` with the decoded unsat core.
Required-application release lookups also return structured rejection details
from Charmhub resolution. At the final failed iteration, assertion tags and
release rejections are translated into an immutable tuple of typed diagnostics.
Generic unresolved-application/integration diagnostics are omitted when a
release diagnostic already explains the same application, and duplicate
diagnostics are removed by structured identity. Every remaining diagnostic is
rendered in deterministic order. Provisional diagnostics are discarded when
another assertion expands the domain and solving continues. Rejections from
speculative neighbour, peer-channel, and base-variant searches remain internal
because an incompatible candidate is normal solver search behavior.

Optimization preparation
~~~~~~~~~~~~~~~~~~~~~~~~

Feasibility expansion creates only the charms and integrations needed to reach
a satisfying assignment. Before optimization, the builder adds a bounded set of
alternatives that can produce a smaller bundle:

- Active charms are paired with each other so one provider can serve multiple
  active consumers.
- Channel and base replacement variants are paired with the active neighbours
  of the charm they replace.
- One additional equivalent charm instance is exposed when it can break a
  bidirectional-interface cycle or satisfy parallel required endpoints that
  cannot share one charm-pair relation.

This preparation operates on the satisfiable graph rather than recursively
expanding every candidate dependency tree.

Key properties
--------------

- **Explicit constraint DSL** -- constraints are compiled to Z3 assertions rather
  than embedded implicitly in scoring or edge expansion.
- **Multi-model** -- all models in a spec are solved simultaneously.
- **Cross-model relations** -- both in-spec and external CMRs are supported.
- **Optimization pass** -- after a satisfying assignment is found, the builder
  first attempts a ``z3.Optimize`` pass (configurable timeout, default 1 minute)
  to find the optimal solution within the bounded domain. If that times out, it
  falls back to iterative descent: a ``z3.Solver`` loop that minimizes charm cost
  first (phase 1), integration count with charm cost fixed (phase 2), and total
  unit count with both earlier costs fixed (phase 3). Each step issues a SAT query
  with a tighter bound.
- **Repeatable expansion** -- candidate ordering and the feasibility solver seed
  are fixed so the chosen expansion path is repeatable.
- **Failure diagnostics** -- when the problem is unsatisfiable, the unsat core is
  decoded into specific constraint tags so callers know exactly what went wrong.
