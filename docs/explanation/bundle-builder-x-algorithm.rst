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

Key properties
--------------

- **Explicit constraint DSL** -- constraints are compiled to Z3 assertions rather
  than embedded implicitly in scoring or edge expansion.
- **Multi-model** -- all models in a spec are solved simultaneously.
- **Cross-model relations** -- both in-spec and external CMRs are supported.
- **Optimization pass** -- after a satisfying assignment is found, a
  ``z3.Optimize`` pass minimizes the number of applications and integrations.
- **Failure diagnostics** -- when the problem is unsatisfiable, the unsat core is
  decoded into specific constraint tags so callers know exactly what went wrong.
