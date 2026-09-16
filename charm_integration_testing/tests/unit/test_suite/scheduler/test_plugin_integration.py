# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests that run the real scheduler plugin in an isolated pytest session.

Unlike test_plugin.py's unit tests (which drive individual hooks directly
against fakes), these use pytest's own ``pytester`` fixture to run a real,
nested pytest session against the actual plugin, so pytest's own internals
(e.g. ``SetupState``, fixture scoping) are genuinely exercised rather than
mocked.
"""

from __future__ import annotations

import re
import textwrap

import pytest
from pytest import Pytester

# Enables the `pytester` fixture used below to run real, isolated pytest
# sessions against the scheduler plugin. Declared here (a test module) rather
# than in conftest.py, since pytest_plugins in a non-top-level conftest is no
# longer supported.
pytest_plugins = ["pytester"]


def test_recovery_bridge_from_a_different_module_does_not_break_fixture_teardown(pytester: Pytester) -> None:
    """A recovery bridge from a different module than the original nextitem must not crash pytest.

    By the time ``pytest_runtest_protocol``'s hookwrapper resumes after
    ``yield``, pytest has already torn the just-finished item down using the
    *original* ``nextitem`` (before recovery decided to inject a bridge).
    That teardown retains any collector scope (e.g. a module-scoped fixture)
    shared between the item and that original ``nextitem``. If the injected
    bridge belongs to a different module, the retained scope is stale for
    whatever pytest actually runs next, and pytest's own
    ``SetupState.setup`` used to assert on it (``previous item was not torn
    down properly``) before the reconciling ``teardown_exact`` call was
    added in ``pytest_runtest_protocol``.

    Scenario: ``test_downgrade_charm`` (module A) skips at setup time
    (simulating missing Test Observer credentials), so the environment stays
    at ``deployed``. The next planned test, ``test_upgrade_charm`` (also
    module A), requires ``neighbor_only``. The only registered transition
    from ``deployed`` to ``neighbor_only`` other than the skipped one is
    ``test_scale``, registered in a *different* module (module B) - forcing
    the cross-module bridge that used to trigger the crash.
    """
    pytester.makeconftest('pytest_plugins = ["test_suite.scheduler.plugin"]')
    pytester.makepyfile(
        test_mod_a=textwrap.dedent(
            """
            import pytest
            from test_suite.scheduler.states import State

            @pytest.fixture(scope="module")
            def mod_fixture_a():
                yield

            @pytest.fixture
            def observer_creds():
                pytest.skip("simulated: test observer creds missing")

            @pytest.mark.state(requires=State.DEPLOYED, provides=State.NEIGHBOR_ONLY)
            def test_downgrade_charm(mod_fixture_a, observer_creds):
                pass

            @pytest.mark.state(requires=State.NEIGHBOR_ONLY, provides=State.DEPLOYED)
            def test_upgrade_charm(mod_fixture_a):
                pass
            """
        )
    )
    pytester.makepyfile(
        test_mod_b=textwrap.dedent(
            """
            import pytest
            from test_suite.scheduler.states import State

            @pytest.fixture(scope="module")
            def mod_fixture_b():
                yield

            @pytest.mark.state(requires=State.DEPLOYED, provides=State.NEIGHBOR_ONLY)
            def test_scale(mod_fixture_b):
                pass
            """
        )
    )

    result = pytester.runpytest("--current-state", "deployed")

    # THEN the recovery bridge (test_scale, injected from module B) and the
    # rest of the plan run cleanly - no AssertionError from pytest's own
    # SetupState, and no error outcome for the injected item.
    result.assert_outcomes(passed=3, skipped=1)
    # AND the terminal reporter's progress percentages stay within 0-100%,
    # proving session.testscollected was kept in sync with the injected
    # bridge item rather than understating the now-longer item list.
    percentages = [int(m) for m in re.findall(r"\[\s*(\d+)%\]", "\n".join(result.outlines))]
    assert percentages, "expected at least one progress percentage in output"
    assert max(percentages) == 100


def test_call_time_skip_via_guard_clause_does_not_halt_remaining_state_marked_tests(pytester: Pytester) -> None:
    """A transition test that skips via a body guard clause must not be treated as a failure.

    Mirrors ``test_upgrade_controller`` in the real suite: it checks a
    precondition as the first statement in the test *body* (not in a
    fixture) and calls ``pytest.skip()`` before doing anything else. Pytest
    classifies this as a *call*-phase skip, but functionally it is identical
    to a setup-time skip: nothing has been mutated yet. Before the fix, the
    scheduler treated any call/teardown-phase skip of a transition test as
    "environment state unknown", incorrectly halting recovery and every
    subsequent state-marked test even though the environment never changed.

    Scenario mirrors
    ``test_recovery_bridge_from_a_different_module_does_not_break_fixture_teardown``
    above, but the skip is a call-phase guard clause instead of a
    setup-phase fixture skip.
    """
    pytester.makeconftest('pytest_plugins = ["test_suite.scheduler.plugin"]')
    pytester.makepyfile(
        test_mod_a=textwrap.dedent(
            """
            import pytest
            from test_suite.scheduler.states import State

            @pytest.mark.state(requires=State.DEPLOYED, provides=State.NEIGHBOR_ONLY)
            def test_downgrade_charm():
                # A guard clause in the test body, exactly like the real
                # test_upgrade_controller: skip before mutating anything.
                pytest.skip("simulated: no downgrade target available")

            @pytest.mark.state(requires=State.NEIGHBOR_ONLY, provides=State.DEPLOYED)
            def test_upgrade_charm():
                pass
            """
        )
    )
    pytester.makepyfile(
        test_mod_b=textwrap.dedent(
            """
            import pytest
            from test_suite.scheduler.states import State

            @pytest.mark.state(requires=State.DEPLOYED, provides=State.NEIGHBOR_ONLY)
            def test_scale():
                pass
            """
        )
    )

    result = pytester.runpytest("--current-state", "deployed")

    # THEN recovery still bridges via an injected test_scale (module B)
    # exactly as it would for a setup-time skip, and test_upgrade_charm
    # still runs - the call-time skip did not halt the state machine.
    result.assert_outcomes(passed=3, skipped=1)
    result.stdout.no_fnmatch_line("*environment state is unknown*")


def test_finalizer_failure_during_cross_module_bridge_reconciliation_gives_a_failing_exit_status(
    pytester: Pytester, caplog: pytest.LogCaptureFixture
) -> None:
    """A retained fixture finalizer that fails during recovery must not silently exit successfully.

    Same cross-module bridge scenario as
    ``test_recovery_bridge_from_a_different_module_does_not_break_fixture_teardown``, but
    module A's module-scoped fixture raises during its own finalizer. Since
    the original ``nextitem`` (also module A) would have kept that fixture's
    scope retained, only the reconciling ``teardown_exact`` call towards the
    injected module B bridge actually tears it down and hits the raising
    finalizer - outside pytest's normal per-item reporting flow. Before the
    fix, this exception would have escaped straight past our hook, likely
    surfacing as a raw traceback/``INTERNALERROR`` instead of a clean,
    accounted-for pytest failure.
    """
    pytester.makeconftest('pytest_plugins = ["test_suite.scheduler.plugin"]')
    pytester.makepyfile(
        test_mod_a=textwrap.dedent(
            """
            import pytest
            from test_suite.scheduler.states import State

            @pytest.fixture(scope="module")
            def mod_fixture_a():
                yield
                raise RuntimeError("simulated finalizer failure")

            @pytest.fixture
            def observer_creds():
                pytest.skip("simulated: test observer creds missing")

            @pytest.mark.state(requires=State.DEPLOYED, provides=State.NEIGHBOR_ONLY)
            def test_downgrade_charm(mod_fixture_a, observer_creds):
                pass

            @pytest.mark.state(requires=State.NEIGHBOR_ONLY, provides=State.DEPLOYED)
            def test_upgrade_charm(mod_fixture_a):
                pass
            """
        )
    )
    pytester.makepyfile(
        test_mod_b=textwrap.dedent(
            """
            import pytest
            from test_suite.scheduler.states import State

            @pytest.mark.state(requires=State.DEPLOYED, provides=State.NEIGHBOR_ONLY)
            def test_scale():
                pass
            """
        )
    )

    result = pytester.runpytest("--current-state", "deployed")

    # THEN pytest's exit status reflects the failure (not a clean, all-green
    # exit despite the finalizer failure)...
    assert result.ret != 0
    # ...our error message explains what happened (propagated via Python
    # logging into this outer test's own captured log records, since the
    # nested in-process pytester run shares the same logging machinery)...
    assert any(
        "Failed to reconcile pytest's setup stack" in record.message and "simulated finalizer failure" in record.message
        for record in caplog.records
    )
    # ...and no raw INTERNALERROR/unhandled traceback leaked past our hook.
    result.stdout.no_fnmatch_line("*INTERNALERROR*")
