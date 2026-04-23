The bundle builder X algorithm
==============================

Bundle builder X is a rewrite of the original :doc:`bundle builder <bundle-builder-algorithm>`
that replaces graph traversal with an SMT solver (`Z3 <https://github.com/Z3Prover/z3>`_).
Instead of exploring a search tree of possible bundles, the solver encodes the entire
problem as a set of logical constraints and finds a satisfying assignment directly.

Why a solver?
-------------

The original bundle builder uses uniform-cost search over an expanding graph of
possible bundle configurations. This works, but has limitations:

- The search space grows combinatorially with the number of charms and endpoints.
- Constraint types like "at most one of these three endpoints" or "the charm on
  the other end of this endpoint must share the same channel track" are awkward to
  express as graph edge weights.
- Multi-model specs with cross-model relations add another dimension to the search.

An SMT solver handles all of these naturally. Constraints are first-class: you
declare what must hold and the solver finds a valid assignment, or proves none
exists.

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
   decoded to determine what went wrong. The builder then expands the domain,
   typically by fetching a new charm that can fulfill an unsatisfied endpoint,
   and loops back to step 2. If the problem is ``sat``, an optimization pass
   minimizes the number of applications and integrations, and the result is
   extracted into concrete ``Bundle`` objects.

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

When the solver returns ``unsat``, the builder inspects the unsat core and
acts on the highest-priority tag:

- ``APPLICATION_EXISTS`` -- the spec references an application whose charm is
  not yet in the domain. Fetch it from Charmhub.
- ``APPLICATION_INTEGRATION_EXISTS`` -- an explicit integration references
  applications not yet in the domain. Fetch them.
- ``CHARM_ENDPOINT_NON_OPTIONAL`` -- a charm has a non-optional endpoint
  with no compatible charm in the domain. Search Charmhub for a charm that
  provides or requires the matching interface and add it.
- ``ENDPOINT_COUNT_MATCHES_INTEGRATIONS`` / ``PEER_CHANNEL_MISMATCH`` --
  structural mismatches that indicate a constraint conflict.

This loop is bounded (default 100 iterations). If the domain cannot be
expanded further and the problem is still unsatisfiable, the builder raises
``UncompletableBundleError`` with the decoded unsat core.

Differences from the original bundle builder
---------------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Aspect
     - Original (v5/v9)
     - X (v10)
   * - Algorithm
     - Uniform-cost graph search
     - Z3 SMT solver
   * - Constraint support
     - Implicit in scoring and edge expansion
     - Explicit DSL compiled to Z3 assertions
   * - Multi-model
     - Single model per invocation
     - All models solved simultaneously
   * - Cross-model relations
     - Not supported
     - In-spec and external CMRs
   * - Optimization
     - Score-based node selection (UCS)
     - ``z3.Optimize`` minimization pass
   * - Failure diagnostics
     - "No valid bundle found"
     - Decoded unsat core with specific constraint tags
