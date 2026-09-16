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

    By the time the hookwrapper resumes after ``yield``, pytest has already
    torn the item down using the *original* ``nextitem``, retaining any
    collector scope (e.g. a module-scoped fixture) shared with it. If the
    injected bridge belongs to a different module, that scope is stale and
    pytest's ``SetupState.setup`` used to assert on it before the
    reconciling ``teardown_exact`` call was added.

    Scenario: ``test_downgrade_charm`` (module A) skips at setup, so the
    environment stays at ``deployed``. ``test_upgrade_charm`` (module A)
    needs ``neighbor_only``, reachable only via ``test_scale`` in a
    *different* module (B) - forcing the cross-module bridge that used to
    trigger the crash.
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

    # The recovery bridge (test_scale, from module B) and the rest of the plan
    # run cleanly - no SetupState assertion, no error outcome for the injected item.
    result.assert_outcomes(passed=3, skipped=1)
    # Progress percentages stay within 0-100%, proving testscollected was kept
    # in sync with the injected bridge item.
    percentages = [int(m) for m in re.findall(r"\[\s*(\d+)%\]", "\n".join(result.outlines))]
    assert percentages, "expected at least one progress percentage in output"
    assert max(percentages) == 100


def test_call_time_skip_via_guard_clause_does_not_halt_remaining_state_marked_tests(pytester: Pytester) -> None:
    """A transition test that skips via a body guard clause must not be treated as a failure.

    Mirrors ``test_upgrade_controller`` in the real suite: it checks a
    precondition as the first statement in the test body and calls
    ``pytest.skip()`` before doing anything else - pytest classifies this as
    a *call*-phase skip, but functionally it's identical to a setup-time
    skip. Before the fix, the scheduler treated any call/teardown-phase skip
    as "environment state unknown", incorrectly halting recovery and every
    subsequent state-marked test.

    Same scenario as the cross-module bridge test above, but the skip is a
    call-phase guard clause instead of a setup-phase fixture skip.
    """
    pytester.makeconftest('pytest_plugins = ["test_suite.scheduler.plugin"]')
    pytester.makepyfile(
        test_mod_a=textwrap.dedent(
            """
            import pytest
            from test_suite.scheduler.states import State

            @pytest.mark.state(requires=State.DEPLOYED, provides=State.NEIGHBOR_ONLY)
            def test_downgrade_charm():
                # Guard clause in the test body, like the real test_upgrade_controller:
                # skip before mutating anything.
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

    # Recovery still bridges via injected test_scale (module B) and test_upgrade_charm
    # still runs - the call-time skip did not halt the state machine.
    result.assert_outcomes(passed=3, skipped=1)
    result.stdout.no_fnmatch_line("*environment state is unknown*")


def test_finalizer_failure_during_cross_module_bridge_reconciliation_gives_a_failing_exit_status(
    pytester: Pytester, caplog: pytest.LogCaptureFixture
) -> None:
    """A retained fixture finalizer that fails during recovery must not silently exit successfully.

    Same cross-module bridge scenario as above, but module A's module-scoped
    fixture raises during its own finalizer. Only the reconciling
    ``teardown_exact`` call towards the injected module B bridge tears it
    down, outside pytest's normal per-item reporting flow - before the fix
    this would have surfaced as a raw traceback/``INTERNALERROR`` instead of
    a clean, accounted-for failure.
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

    # Exit status reflects the failure (not a clean, all-green exit).
    assert result.ret != 0
    # Our error message explains what happened (via logging, shared with this
    # outer test's captured records since pytester runs in-process).
    assert any(
        "Failed to reconcile pytest's setup stack" in record.message and "simulated finalizer failure" in record.message
        for record in caplog.records
    )
    # No raw INTERNALERROR/unhandled traceback leaked past our hook.
    result.stdout.no_fnmatch_line("*INTERNALERROR*")


def test_finalizer_calling_pytest_skip_during_reconciliation_gives_a_failing_exit_status(
    pytester: Pytester, caplog: pytest.LogCaptureFixture
) -> None:
    """A retained fixture finalizer that calls ``pytest.skip()`` must be handled like any other failure.

    Same scenario as the ``RuntimeError``-raising finalizer test above, but
    the finalizer calls ``pytest.skip()`` instead. ``Skipped`` (like
    ``Failed``) deliberately derives from ``BaseException`` rather than
    ``Exception`` so ordinary pytest internals don't catch it; a naive
    ``except Exception`` around the reconciling ``teardown_exact`` call would
    let it escape uncaught as an unexplained ``INTERNALERROR``.
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
                pytest.skip("simulated: finalizer decided to skip")

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

    # Exit status still reflects the failure, error message explains it, and
    # no raw INTERNALERROR/unhandled traceback leaked past our hook.
    assert result.ret != 0
    assert any(
        "Failed to reconcile pytest's setup stack" in record.message and "finalizer decided to skip" in record.message
        for record in caplog.records
    )
    result.stdout.no_fnmatch_line("*INTERNALERROR*")


def test_finalizer_failure_stops_the_run_under_maxfail(pytester: Pytester) -> None:
    """A reconciliation finalizer failure must stop the run under ``--maxfail``, like a normal failure would.

    Same cross-module bridge + raising finalizer scenario as above, but with
    an extra unrelated unmarked test and ``--maxfail=1``. Bumping
    ``session.testsfailed`` alone (without also setting
    ``session.shouldfail``, mirroring pytest's own accounting) makes the
    exit status nonzero but doesn't stop the run early.
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
    pytester.makepyfile(
        test_mod_c=textwrap.dedent(
            """
            def test_unrelated_and_unmarked():
                pass
            """
        )
    )

    result = pytester.runpytest("-v", "--current-state", "deployed", "--maxfail", "1")

    # The run stops before reaching the unrelated, unmarked test in module C.
    result.stdout.no_fnmatch_line("*test_unrelated_and_unmarked*")
    assert result.ret != 0
