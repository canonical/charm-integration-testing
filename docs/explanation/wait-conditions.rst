Wait Conditions and Status Detection
=====================================

This document explains the internal mechanisms for detecting when Juju models reach stable states during testing.

Core Wait Algorithm
-------------------

The wait mechanism uses consecutive success counting to ensure stability:

1. **Poll Loop**: Fetches Juju status at fixed intervals
2. **Condition Evaluation**: Each status is evaluated against a predicate function
3. **Success Counting**: Counter increments on success, resets to zero on failure, exits when reaching threshold
4. **Timeout**: Exits with failure if maximum time exceeded
5. **Timing Compensation**: Sleep duration accounts for status fetch time, maintaining consistent polling intervals

A model must remain in the desired state for multiple consecutive checks before signaling success.

The ``assert_idle`` Fixture
----------------------------

Every test automatically runs the ``assert_idle`` fixture before execution. This fixture:

- Runs as an autouse fixture, executing before each test function
- Calls ``idle_for_period()`` to verify the model is in ``active`` state
- Skips the test (not fails) if the model is not idle
- Acts as a precondition check rather than a test assertion

This ensures tests only run against models in a known good state, preventing cascading failures from previous test issues.

Timeout Configuration
---------------------

Different wait contexts use different timeout and count values:

**For assert_idle (pre-test checks)**: ``count=5`` with a 30-second timeout

This provides fast failure detection before tests run. The shorter timeout quickly identifies models that aren't ready, causing the test to skip rather than wait indefinitely.

**For end-of-test waits**: ``count=30`` with a 15-minute timeout

This accommodates the 2-minute update-status hook interval, allowing charms with reconciler patterns multiple opportunities to stabilize. The model must remain in the desired state for approximately 30 consecutive seconds (at 1-second polling intervals) before the wait succeeds.

Wait Condition Implementations
------------------------------

Different wait conditions check for different stability criteria:

**all_statuses_are_in (idle_for_period)**
  Checks that all application and unit statuses match expected values (typically ``active``). Used as the baseline stability check.

**all_statuses_are_in (wait_application_settled)**
  Same algorithm but accepts multiple statuses (``active`` or ``blocked``), allowing for configurations where blocked is a valid stable state.

**applications_are_scaled**
  Verifies application scale matches desired units by counting units with agent status ``idle`` or ``executing``. Distinguishes between units existing vs. units being ready.

**units_have_message**
  Pattern matches against unit workload status messages. Used for state-specific waits (e.g., vault initialization messages).

**applications_are_removed / integrations_are_removed**
  Negative checks confirming absence of entities after removal operations.

Status Flapping and Detection Reliability
------------------------------------------

Charms execute hooks periodically (``update-status``) which can cause status transitions. If status changes during the consecutive success window, the counter resets.

**Example** (``count=3``, 1s polling):

::

   T=0s:  active (counter=1)
   T=1s:  active (counter=2)
   T=2s: update-status fires → maintenance (counter=0, restart)
   T=3s: maintenance (counter=0))
   T=4s: maintenance (counter=0))
   T=5s: active (counter=1)
   T=6s: active (counter=2)
   T=7s: active (counter=3) → SUCCESS

**Trade-offs**: Higher count values increase resistance to flaps but require longer stable periods. Longer update-status intervals reduce flapping but slow charm feedback.

Update Status Hook and Reconciler Patterns
------------------------------------------

The ``update-status`` hook (default: 5 minutes, test override: 2 minutes) creates periodic timing that interacts with wait detection. Shorter intervals increase flapping risk but provide faster feedback.

**Reconciler Pattern Impact**

Many charms use ``update-status`` to actively reconcile state, not just report it. Each execution may legitimately cause status transitions as the charm:

- Checks actual vs. desired state
- Performs corrective actions if drift detected
- Processes queued operations from previous hooks

This challenges consecutive success counting: charms need multiple reconciliation cycles to stabilize, with each cycle causing transitions that reset the counter. With ``count=30`` and 1-second polling, the model must remain stable for approximately 30 consecutive seconds. Combined with a 2-minute update-status interval, this allows reconciliation opportunities while ensuring genuine stability before proceeding.

**CI/CD Strategy**: 2-minute update-status intervals with ``count=20-30`` balances giving reconciler charms sufficient opportunities to stabilize while maintaining reasonable test execution times.

Error State Tracking
--------------------

The wait mechanism tracks two states: **last_wait_state** (most recent) and **noncompliant_wait_state** (last failure). Timeout errors report the noncompliant state, not the last state.

**Rationale**: A charm might error, recover to active, then timeout due to insufficient consecutive successes. Without tracking, the error shows "timed out while active" which hides the actual problem. With tracking, the error shows the last problematic state for debugging context.
