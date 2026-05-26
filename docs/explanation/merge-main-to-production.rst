Merge main to production
========================

The ``Merge Main to Production`` workflow
(``.github/workflows/merge-main-to-production.yaml``) is the gate that
promotes code from the ``main`` branch to the ``production`` branch.  It
runs on a schedule (every 12 hours) and can be triggered manually via
``workflow_dispatch``.

Workflow stages
---------------

The workflow has three sequential jobs:

1. **check-for-changes** — compares ``origin/main`` and
   ``origin/production``.  If the branches are identical, the remaining
   jobs are skipped entirely.

2. **run-all-tests** — calls the reusable
   ``charm-testing-integration-tests.yaml`` workflow with
   ``full_test_suite: true``.  This runs the full integration test matrix
   with testmon disabled, ensuring every test is exercised before code
   reaches production.

3. **merge-main-to-production** — fast-forward merges ``main`` into
   ``production``.  The merge is ``--ff-only``, so it will fail if
   ``production`` has diverged from ``main`` (which should not happen
   under normal operation).

Why ``full_test_suite`` matters
-------------------------------

During pull-request checks, testmon is enabled so only tests affected by
the changed code run.  This keeps PR feedback fast.

The merge workflow intentionally sets ``full_test_suite: true``, which
disables testmon and forces every test to execute.  This catches
cross-test interactions and environment-dependent regressions that
incremental runs might miss, providing a final safety net before
production promotion.

Concurrency and idempotency
----------------------------

The workflow uses a concurrency group scoped to the workflow name and ref:

.. code:: yaml

   concurrency:
     group: ${{ github.workflow }}-${{ github.ref }}
     cancel-in-progress: false

``cancel-in-progress: false`` means a second trigger (e.g. a manual
dispatch while the scheduled run is in progress) will **queue** rather
than cancel the running workflow.  This prevents a race where a partial
merge could leave ``production`` in an intermediate state.

The ``--ff-only`` merge strategy ensures the operation is idempotent: if
``main`` and ``production`` are already at the same commit, the merge is
a no-op.

Secret handling
---------------

The ``run-all-tests`` job uses ``secrets: inherit`` to forward all
repository secrets to the reusable workflow.  The reusable workflow in
turn passes ``STG_TEST_OBSERVER_TOKEN`` to the downstream
``charm-testing-integration-test-assert-pass.yaml`` workflow, which uses
it to create and assert Test Observer executions against the staging API.
