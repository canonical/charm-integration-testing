# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for scheduler/plugin.py - _mark_as_injected and _build_execution_plan."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Generator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_suite.scheduler import plugin as _plugin_module
from test_suite.scheduler.graph import StateGraph, StateTransition
from test_suite.scheduler.plugin import (
    _build_execution_plan,
    _mark_as_injected,
    _UnreachableStateError,
    pytest_runtest_setup,
    pytest_sessionfinish,
)
from test_suite.scheduler.states import State

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _graph_and_all(
    *edges: tuple[State, State, pytest.Item],
) -> tuple[StateGraph, dict[StateTransition, list[pytest.Item]]]:
    """Build a StateGraph and matching all_transitions dict from (from, to, item) tuples.

    Keeping these two data structures consistent is critical: the scheduler uses
    the StateGraph for Dijkstra and all_transitions to retrieve actual bridge items.
    Passing them through this helper avoids divergence.
    """
    graph = StateGraph()
    all_transitions: dict[StateTransition, list[pytest.Item]] = defaultdict(list)
    for from_s, to_s, item in edges:
        t = StateTransition(from_s, to_s)
        graph.register_transition(t, item)
        all_transitions[t].append(item)
    return graph, all_transitions


# ---------------------------------------------------------------------------
# Tests for _mark_as_injected
# ---------------------------------------------------------------------------


class TestMarkAsInjected:
    def test_adds_injected_marker(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN an unmarked item
        item = make_item("test_foo")

        # WHEN marked as injected
        _mark_as_injected(item)

        # THEN the injected marker is present
        assert item.get_closest_marker("injected") is not None

    def test_prefixes_name_with_injected(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN an item with a known name
        item = make_item("test_foo")
        original_name = item.name

        # WHEN marked as injected
        _mark_as_injected(item)

        # THEN the name is prefixed
        assert item.name == f"[injected] {original_name}"

    def test_prefixes_nodeid_with_injected(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN an item with a known node ID
        item = make_item("test_foo")
        original_nodeid = item.nodeid

        # WHEN marked as injected
        _mark_as_injected(item)

        # THEN the node ID is prefixed
        assert item.nodeid == f"[injected] {original_nodeid}"

    def test_idempotent_second_call_is_noop(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN an item that has already been marked as injected
        item = make_item("test_foo")
        _mark_as_injected(item)
        name_after_first = item.name
        nodeid_after_first = item.nodeid

        # WHEN marked as injected a second time
        _mark_as_injected(item)

        # THEN neither the name nor the nodeid changes (prefix not doubled)
        assert item.name == name_after_first
        assert item.nodeid == nodeid_after_first


# ---------------------------------------------------------------------------
# Tests for _build_execution_plan
# ---------------------------------------------------------------------------


class TestBuildExecutionPlan:
    def test_already_at_required_state_runs_directly(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a pure test requiring DEPLOYED and current state already DEPLOYED
        pure = make_item("test_pure")

        # WHEN the plan is built
        plan = _build_execution_plan(
            current_state=State.DEPLOYED,
            pure_clusters={State.DEPLOYED: [pure]},
            selected_transitions=defaultdict(list),
            all_transitions=defaultdict(list),
            full_graph=StateGraph(),
        )

        # THEN the pure test is in the plan without any bridge injection
        assert plan == [pure]
        assert pure.get_closest_marker("injected") is None

    def test_bridge_injected_when_gap_exists(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN current state is EMPTY_MODEL but selected test requires DEPLOYED
        deploy = make_item("test_deploy")
        teardown = make_item("test_teardown")

        graph, all_transitions = _graph_and_all(
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
            (State.DEPLOYED, State.NEIGHBOR_ONLY, teardown),
        )

        # WHEN the plan is built - only teardown is user-selected
        plan = _build_execution_plan(
            current_state=State.EMPTY_MODEL,
            pure_clusters=defaultdict(list),
            selected_transitions=defaultdict(list, {StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY): [teardown]}),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN deploy is injected before teardown
        assert deploy in plan
        assert teardown in plan
        assert plan.index(deploy) < plan.index(teardown)

    def test_injected_bridge_is_labelled(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a bridge that must be injected automatically (not user-selected)
        deploy = make_item("test_deploy")
        teardown = make_item("test_teardown")

        graph, all_transitions = _graph_and_all(
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
        )

        # WHEN the plan is built with deploy absent from selected_transitions
        _build_execution_plan(
            current_state=State.EMPTY_MODEL,
            pure_clusters=defaultdict(list),
            selected_transitions=defaultdict(list, {StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY): [teardown]}),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN the bridge item is labelled as injected
        assert deploy.get_closest_marker("injected") is not None
        assert deploy.name.startswith("[injected]")
        assert deploy.nodeid.startswith("[injected]")

    def test_selected_transition_not_labelled_as_injected(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN deploy is explicitly user-selected (present in selected_transitions)
        deploy = make_item("test_deploy")

        graph, all_transitions = _graph_and_all(
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
        )

        # WHEN the plan is built with deploy in selected_transitions
        _build_execution_plan(
            current_state=State.EMPTY_MODEL,
            pure_clusters=defaultdict(list),
            selected_transitions=defaultdict(list, {StateTransition(State.EMPTY_MODEL, State.DEPLOYED): [deploy]}),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN deploy is never labelled as injected
        assert deploy.get_closest_marker("injected") is None

    def test_pure_tests_run_before_transition_at_same_state(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a pure test and a transition both departing from DEPLOYED
        pure = make_item("test_pure")
        teardown = make_item("test_teardown")

        graph, all_transitions = _graph_and_all(
            (State.DEPLOYED, State.NEIGHBOR_ONLY, teardown),
        )

        # WHEN the plan is built
        plan = _build_execution_plan(
            current_state=State.DEPLOYED,
            pure_clusters={State.DEPLOYED: [pure]},
            selected_transitions=defaultdict(list, {StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY): [teardown]}),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN the pure test runs before the transition
        assert plan.index(pure) < plan.index(teardown)

    def test_multiple_pure_tests_at_same_state_all_appear(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN three pure tests all requiring DEPLOYED
        pures = [make_item(f"test_pure_{i}") for i in range(3)]

        # WHEN the plan is built
        plan = _build_execution_plan(
            current_state=State.DEPLOYED,
            pure_clusters={State.DEPLOYED: pures},
            selected_transitions=defaultdict(list),
            all_transitions=defaultdict(list),
            full_graph=StateGraph(),
        )

        # THEN every pure test appears exactly once
        for pure in pures:
            assert pure in plan
        assert len(plan) == len(pures)

    def test_multiple_transitions_same_edge_each_gets_fresh_environment(
        self, make_item: Callable[..., pytest.Item]
    ) -> None:
        """Two selected tests on the same edge must each start from a newly-prepared env.

        The scheduler must navigate back to DEPLOYED (via the redeploy bridge) after
        teardown_1 leaves the model in NEIGHBOR_ONLY, before running teardown_2.
        """
        # GIVEN two teardown variants and the bridging transitions
        deploy = make_item("test_deploy")
        teardown_1 = make_item("test_teardown_1")
        teardown_2 = make_item("test_teardown_2")
        redeploy = make_item("test_redeploy")

        graph, all_transitions = _graph_and_all(
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
            (State.DEPLOYED, State.NEIGHBOR_ONLY, teardown_1),
            (State.NEIGHBOR_ONLY, State.DEPLOYED, redeploy),
        )
        # Both teardown variants cover the same edge in all_transitions
        all_transitions[StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY)].append(teardown_2)

        # WHEN both teardowns are user-selected
        plan = _build_execution_plan(
            current_state=State.EMPTY_MODEL,
            pure_clusters=defaultdict(list),
            selected_transitions=defaultdict(
                list,
                {StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY): [teardown_1, teardown_2]},
            ),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN both teardowns appear in the plan
        assert teardown_1 in plan
        assert teardown_2 in plan

        # AND the redeploy bridge is injected between them
        assert redeploy in plan
        assert plan.index(teardown_1) < plan.index(redeploy)
        assert plan.index(redeploy) < plan.index(teardown_2)

        # AND bridge items are labelled injected; user-selected teardowns are not
        assert redeploy.get_closest_marker("injected") is not None
        assert teardown_1.get_closest_marker("injected") is None
        assert teardown_2.get_closest_marker("injected") is None

    def test_fork_both_branches_scheduled_with_re_navigation(self, make_item: Callable[..., pytest.Item]) -> None:
        """Two transitions depart from DEPLOYED to different states.

        After the first runs, the scheduler must navigate back to DEPLOYED via
        the redeploy bridge before it can run the second.
        """
        # GIVEN a deploy bridge, two diverging transitions, and a redeploy bridge
        deploy = make_item("test_deploy")
        teardown = make_item("test_teardown")  # DEPLOYED → NEIGHBOR_ONLY
        upgrade = make_item("test_upgrade")  # DEPLOYED → NO_CONTROLLER
        redeploy = make_item("test_redeploy")  # NEIGHBOR_ONLY → DEPLOYED

        graph, all_transitions = _graph_and_all(
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
            (State.DEPLOYED, State.NEIGHBOR_ONLY, teardown),
            (State.DEPLOYED, State.NO_CONTROLLER, upgrade),
            (State.NEIGHBOR_ONLY, State.DEPLOYED, redeploy),
        )

        # WHEN both diverging transitions are user-selected
        plan = _build_execution_plan(
            current_state=State.EMPTY_MODEL,
            pure_clusters=defaultdict(list),
            selected_transitions=defaultdict(
                list,
                {
                    StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY): [teardown],
                    StateTransition(State.DEPLOYED, State.NO_CONTROLLER): [upgrade],
                },
            ),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN both branches are present
        assert teardown in plan
        assert upgrade in plan

        # AND the plan is non-trivially reordered (there must be a bridge between them)
        assert plan.index(teardown) != plan.index(upgrade) - 1 or redeploy in plan

    def test_no_path_raises_unreachable_state_error(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a selected test requiring DEPLOYED but no path exists in the suite
        teardown = make_item("test_teardown")

        # WHEN the plan is built against an empty graph
        # THEN _UnreachableStateError is raised
        with pytest.raises(_UnreachableStateError, match="No path from state"):
            _build_execution_plan(
                current_state=State.EMPTY_MODEL,
                pure_clusters=defaultdict(list),
                selected_transitions=defaultdict(
                    list, {StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY): [teardown]}
                ),
                all_transitions=defaultdict(list),
                full_graph=StateGraph(),
            )

    def test_empty_selection_returns_empty_plan(self) -> None:
        # GIVEN no user-selected items at all
        # WHEN the plan is built
        plan = _build_execution_plan(
            current_state=State.DEPLOYED,
            pure_clusters=defaultdict(list),
            selected_transitions=defaultdict(list),
            all_transitions=defaultdict(list),
            full_graph=StateGraph(),
        )

        # THEN the plan is empty
        assert plan == []

    def test_multi_hop_bridge_injected_for_distant_state(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN current state is NO_CONTROLLER, selected test requires DEPLOYED
        # and it takes two transitions to get there
        bootstrap = make_item("test_bootstrap")  # NO_CONTROLLER → EMPTY_MODEL
        deploy = make_item("test_deploy")  # EMPTY_MODEL → DEPLOYED
        pure = make_item("test_pure")

        graph, all_transitions = _graph_and_all(
            (State.NO_CONTROLLER, State.EMPTY_MODEL, bootstrap),
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
        )

        # WHEN the plan is built
        plan = _build_execution_plan(
            current_state=State.NO_CONTROLLER,
            pure_clusters={State.DEPLOYED: [pure]},
            selected_transitions=defaultdict(list),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN both bridge transitions are injected before the pure test
        assert bootstrap in plan
        assert deploy in plan
        assert pure in plan
        assert plan.index(bootstrap) < plan.index(deploy)
        assert plan.index(deploy) < plan.index(pure)

        # AND both are labelled as injected
        assert bootstrap.get_closest_marker("injected") is not None
        assert deploy.get_closest_marker("injected") is not None

    def test_bridge_item_can_be_injected_multiple_times(self, make_item: Callable[..., pytest.Item]) -> None:
        """A bridge-only item in all_transitions that is NOT in selected_transitions
        must be re-injectable: it must not be added to `scheduled`, so the same
        item object may appear more than once in the plan when the scheduler crosses
        the same edge more than once.
        """
        # GIVEN two selected teardown tests require the same (DEPLOYED→NEIGHBOR_ONLY) edge
        deploy = make_item("test_deploy")
        teardown_1 = make_item("test_teardown_1")
        teardown_2 = make_item("test_teardown_2")
        redeploy = make_item("test_redeploy")

        graph, all_transitions = _graph_and_all(
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
            (State.DEPLOYED, State.NEIGHBOR_ONLY, teardown_1),
            (State.NEIGHBOR_ONLY, State.DEPLOYED, redeploy),
        )
        all_transitions[StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY)].append(teardown_2)

        plan = _build_execution_plan(
            current_state=State.EMPTY_MODEL,
            pure_clusters=defaultdict(list),
            selected_transitions=defaultdict(
                list,
                {StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY): [teardown_1, teardown_2]},
            ),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN deploy appears once (it is a pure bridge injected for the first traversal)
        # and redeploy appears once (injected to re-navigate back)
        assert plan.count(teardown_1) == 1
        assert plan.count(teardown_2) == 1
        assert redeploy in plan


# ---------------------------------------------------------------------------
# Helpers for hook tests
# ---------------------------------------------------------------------------


def _make_report(when: str = "call", failed: bool = False) -> Any:
    """Return a minimal test report substitute."""
    return SimpleNamespace(when=when, failed=failed)


def _drive_makereport(item: pytest.Item, call: Any, report: Any) -> None:
    """Drive pytest_runtest_makereport (a hookwrapper generator) to completion.

    The hookwrapper yields once; we send back a mock outcome that returns
    *report* from ``get_result()``.

    The function is typed ``-> None`` for pytest's hook protocol, so we cast
    the return value to the underlying generator type to satisfy mypy.
    """
    gen = cast(
        Generator[None, Any, None],
        _plugin_module.pytest_runtest_makereport(item, call),
    )
    next(gen)  # advance to the yield
    outcome = SimpleNamespace(get_result=lambda: report)
    try:
        gen.send(outcome)
    except StopIteration:
        pass


# ---------------------------------------------------------------------------
# Tests for pytest_runtest_makereport (failure detection)
# ---------------------------------------------------------------------------


class TestPytestRuntestMakereport:
    def test_sets_failed_state_test_on_marked_item_failure(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a state-marked item and a failing call-phase report
        item = make_item("test_deploy", requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
        call = SimpleNamespace(excinfo=SimpleNamespace(type=AssertionError, value=AssertionError()))
        report = _make_report(when="call", failed=True)

        # WHEN the hook runs
        _drive_makereport(item, call, report)

        # THEN the item is recorded as the failed state test
        assert _plugin_module._failed_state_test is item

    def test_does_not_set_for_unmarked_item_failure(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN an item with no state marker
        item = make_item("test_something")
        call = SimpleNamespace(excinfo=SimpleNamespace(type=AssertionError, value=AssertionError()))
        report = _make_report(when="call", failed=True)

        # WHEN the hook runs
        _drive_makereport(item, call, report)

        # THEN no failure is recorded - unmarked tests never halt the state machine
        assert _plugin_module._failed_state_test is None

    def test_does_not_set_when_report_not_failed(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a state-marked item but a passing report
        item = make_item("test_deploy", requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
        call = SimpleNamespace(excinfo=None)
        report = _make_report(when="call", failed=False)

        _drive_makereport(item, call, report)

        assert _plugin_module._failed_state_test is None

    def test_does_not_overwrite_once_already_set(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a first test has already failed
        first = make_item("test_first", requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
        _plugin_module._failed_state_test = first

        # WHEN a second state-marked test also fails
        second = make_item("test_second", requires=State.DEPLOYED, provides=State.NEIGHBOR_ONLY)
        call = SimpleNamespace(excinfo=SimpleNamespace(type=AssertionError, value=AssertionError()))
        report = _make_report(when="call", failed=True)
        _drive_makereport(second, call, report)

        # THEN the original failing test is preserved
        assert _plugin_module._failed_state_test is first


# ---------------------------------------------------------------------------
# Tests for pytest_runtest_setup (halting downstream tests)
# ---------------------------------------------------------------------------


class TestPytestRuntestSetup:
    def test_skips_state_marked_test_after_failure(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a previous test has failed
        failed = make_item("test_deploy", requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
        _plugin_module._failed_state_test = failed

        # AND a subsequent state-marked test
        subsequent = make_item("test_integration", requires=State.DEPLOYED)

        # THEN running setup for the subsequent test raises skip
        with pytest.raises(pytest.skip.Exception):
            pytest_runtest_setup(subsequent)

    def test_does_not_skip_the_failing_item_itself(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN the failed item is the same item being set up
        failed = make_item("test_deploy", requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
        _plugin_module._failed_state_test = failed

        # THEN setup is allowed to proceed (no skip raised)
        pytest_runtest_setup(failed)  # must not raise

    def test_does_not_skip_unmarked_item(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a previous test has failed
        failed = make_item("test_deploy", requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
        _plugin_module._failed_state_test = failed

        # AND an unmarked test
        unmarked = make_item("test_smoke")  # no state marker kwargs

        # THEN setup is allowed to proceed for the unmarked test
        pytest_runtest_setup(unmarked)  # must not raise

    def test_does_nothing_when_no_prior_failure(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN no prior failure
        item = make_item("test_deploy", requires=State.EMPTY_MODEL, provides=State.DEPLOYED)

        # THEN setup proceeds normally
        pytest_runtest_setup(item)  # must not raise

    def test_skip_message_names_the_failing_test(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a named failing test
        failed = make_item("test_deploy", requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
        _plugin_module._failed_state_test = failed

        subsequent = make_item("test_integration", requires=State.DEPLOYED)

        with pytest.raises(pytest.skip.Exception) as exc_info:
            pytest_runtest_setup(subsequent)

        assert "test_deploy" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Tests for pytest_sessionfinish (global cleanup)
# ---------------------------------------------------------------------------


class TestPytestSessionFinish:
    def test_clears_all_collected(self, make_item: Callable[..., pytest.Item]) -> None:
        _plugin_module._all_collected.append(make_item("test_foo"))
        pytest_sessionfinish(session=SimpleNamespace(), exitstatus=0)  # type: ignore[arg-type]
        assert _plugin_module._all_collected == []

    def test_clears_injected_item_ids(self, make_item: Callable[..., pytest.Item]) -> None:
        item = make_item("test_foo")
        _mark_as_injected(item)
        assert _plugin_module._injected_item_ids  # non-empty before
        pytest_sessionfinish(session=SimpleNamespace(), exitstatus=0)  # type: ignore[arg-type]
        assert _plugin_module._injected_item_ids == set()

    def test_clears_failed_state_test(self, make_item: Callable[..., pytest.Item]) -> None:
        _plugin_module._failed_state_test = make_item("test_deploy")
        pytest_sessionfinish(session=SimpleNamespace(), exitstatus=0)  # type: ignore[arg-type]
        assert _plugin_module._failed_state_test is None
