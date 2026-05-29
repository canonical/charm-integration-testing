State-driven test scheduler
============================

The state-driven test scheduler is a pytest plugin
(``test_suite/scheduler/plugin.py``) that reorders test items and
automatically injects bridging transition tests so that every test runs
with its state prerequisites satisfied.

Why a scheduler is needed
-------------------------

The integration test suite is a *DAG* against live
infrastructure.  Each test assumes that a previous test has already
established the required state (e.g. a bootstrapped controller, a created
model, a deployed bundle).  Running tests out of order, or skipping a
required setup step, leaves the environment in the wrong state and causes
cascading failures.

The scheduler solves this by:

1. Building a complete **state graph** from all collected test items.
2. Treating the user's selection as **destinations**.
3. Using **Dijkstra** to find the shortest bridging path between states.
4. **Injecting** any bridging transition tests that the user did not select.
5. **Reordering** the final item list so state prerequisites are satisfied.

Core and leaf tests
-------------------

Tests are split into two classes for testmon integration:

**Core (spine) tests**
   Tagged ``@pytest.mark.core``.  These form the mandatory base chain
   (build bundle, bootstrap controller, create model, deploy, scale,
   teardown) and must run on every session to establish live infrastructure
   state.  The scheduler hides them from testmon so they are never
   deselected.

**Leaf tests**
   Everything without the ``core`` marker.  testmon owns selection of these:
   unchanged leaves are deselected and do not run.  This is where testmon
   provides its speedup.

Hook ordering
-------------

The plugin registers several pytest hooks:

``pytest_ignore_collect`` (``tryfirst=True``)
   Forces collection of every ``.py`` file inside the test-suite package,
   even when testmon would skip the file.  This guarantees the scheduler has
   the complete state graph.

``pytest_itemcollected``
   Captures every collected item into ``_all_collected`` before any
   filtering.

``pytest_collection_modifyitems`` (``hookwrapper``)
   Enforces the core/leaf split:

   - Before ``yield``: removes core items from ``items``.
   - During ``yield``: testmon deselects unchanged leaves.
   - After ``yield``: restores core items, then runs the scheduler on the
     combined set.

   Without ``--testmon``, the wrapper simply yields and schedules the full
   selection.

``pytest_runtest_makereport``
   Detects failed state-marked tests and halts the state machine.  Once a
   state-marked test fails, all subsequent state-marked tests are skipped
   because the environment is indeterminate.

``pytest_report_teststatus``
   Renders the ``[injected]`` label for bridge tests without mutating the
   nodeid.  This is critical for testmon compatibility: testmon keys its
   fingerprints on the nodeid, so mutations would cause perpetual
   re-selection.

``pytest_runtest_setup``
   Skips state-marked tests after a transition failure.

``pytest_sessionfinish``
   Resets module-level state for process reuse.  Also treats an empty
   testmon session (all leaves deselected, no core tests) as a success
   rather than ``NO_TESTS_COLLECTED``.

Backtracking algorithm
-----------------------

The scheduling algorithm uses exhaustive backtracking to find a valid
ordering of destination states:

1. Compute remaining (unscheduled) destinations.
2. For each destination, find the shortest Dijkstra path from the current
   state.
3. Inject bridging transitions along the path.
4. Run user-selected tests at the destination.
5. Recurse with the new state.
6. If a branch leads to a dead end, backtrack and try another destination
   order.

Dead-end memoization and an in-flight cycle guard guarantee termination on
cyclic graphs.  Bridge items are recorded by ``id()`` during backtracking
but only labelled as injected after the final plan is committed, so
abandoned branches cannot permanently mark pytest items.

The ``--current-state`` option
-------------------------------

The ``--current-state`` CLI option tells the scheduler the current
environment state before any tests run.  Valid values match the ``State``
enum:

- ``no_bundle`` (default)
- ``no_controller``
- ``no_model``
- ``empty_model``
- ``deployed``
- ``neighbor_only``
- ``deployed_with_old_revision``
- ``deployed_with_upgraded_controller``

Use a non-default value when resuming a partial run or iterating locally
against an already-deployed model to skip expensive setup transitions.
