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
    _disambiguate_repeated_items,
    _duplicate_item_for_repeat,
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

    def test_sets_nodeid_to_injected_name(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN an item with a known name
        item = make_item("test_foo")

        # WHEN marked as injected
        _mark_as_injected(item)

        # THEN the node ID is the bare function name prefixed with [injected],
        # not the full file path, so JUnit XML renders it as a short name
        assert item.nodeid == "[injected] test_foo"

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
# Tests for _duplicate_item_for_repeat and _disambiguate_repeated_items
# ---------------------------------------------------------------------------


class TestDuplicateItemForRepeat:
    def test_duplicate_has_distinct_name_and_nodeid(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN an item
        item = make_item("test_foo")

        # WHEN duplicated for its 2nd occurrence
        duplicate = _duplicate_item_for_repeat(item, 2)

        # THEN the duplicate's name and nodeid differ from the original's
        assert duplicate.name != item.name
        assert duplicate.nodeid != item.nodeid
        assert duplicate.name.startswith(item.name)
        assert duplicate.nodeid.startswith(item.nodeid)

    def test_duplicate_is_not_the_same_object(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN an item
        item = make_item("test_foo")

        # WHEN duplicated
        duplicate = _duplicate_item_for_repeat(item, 2)

        # THEN it is an independent object, not an alias
        assert duplicate is not item

    def test_original_item_is_unmodified(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN an item with known name/nodeid
        item = make_item("test_foo")
        original_name = item.name
        original_nodeid = item.nodeid

        # WHEN duplicated
        _duplicate_item_for_repeat(item, 2)

        # THEN the original item's name/nodeid are untouched
        assert item.name == original_name
        assert item.nodeid == original_nodeid

    def test_different_occurrences_produce_different_nodeids(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN an item duplicated for two different occurrence numbers
        item = make_item("test_foo")

        # WHEN duplicated twice with different occurrence numbers
        second = _duplicate_item_for_repeat(item, 2)
        third = _duplicate_item_for_repeat(item, 3)

        # THEN each duplicate has its own, distinct nodeid
        assert second.nodeid != third.nodeid


class TestDisambiguateRepeatedItems:
    def test_single_occurrence_item_is_unchanged(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a plan with each item appearing once
        a = make_item("test_a")
        b = make_item("test_b")

        # WHEN disambiguated
        result = _disambiguate_repeated_items([a, b])

        # THEN both items pass through untouched (same objects, same nodeids)
        assert result == [a, b]
        assert result[0] is a
        assert result[1] is b

    def test_repeated_item_second_occurrence_is_duplicated(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a plan where the same item object appears twice
        item = make_item("test_teardown")

        # WHEN disambiguated
        result = _disambiguate_repeated_items([item, item])

        # THEN the first occurrence is untouched, the second is a distinct duplicate
        assert result[0] is item
        assert result[1] is not item
        assert result[0].nodeid != result[1].nodeid

    def test_three_occurrences_all_get_unique_nodeids(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a plan where the same item object appears three times
        item = make_item("test_teardown")

        # WHEN disambiguated
        result = _disambiguate_repeated_items([item, item, item])

        # THEN all three occurrences have distinct nodeids
        assert len({occ.nodeid for occ in result}) == 3

    def test_preserves_plan_order_and_length(self, make_item: Callable[..., pytest.Item]) -> None:
        # GIVEN a plan mixing a repeated item with unique ones
        a = make_item("test_a")
        b = make_item("test_b")

        # WHEN disambiguated
        result = _disambiguate_repeated_items([a, b, a])

        # THEN the length and relative order are preserved
        assert len(result) == 3
        assert result[0] is a
        assert result[1] is b
        assert result[2] is not a  # repeat of `a`, disambiguated


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

    def test_selected_transition_not_labelled_when_abandoned_branch_tried_it_as_bridge(
        self, make_item: Callable[..., pytest.Item]
    ) -> None:
        """Regression: user-selected transition must not be marked injected due to a dead-end branch.

        Scenario mirrors the real suite run with --current-state no_bundle:
        * Graph: NO_CONTROLLER → EMPTY_MODEL (bridge) → DEPLOYED (deploy) → NEIGHBOR_ONLY (teardown) → DEPLOYED (redeploy)
        * Starting state: NO_CONTROLLER
        * User selection: deploy (EMPTY_MODEL→DEPLOYED), teardown (DEPLOYED→NEIGHBOR_ONLY), redeploy (NEIGHBOR_ONLY→DEPLOYED)
        * Sorted destination order: deployed < empty_model < neighbor_only
        * The scheduler first tries DEPLOYED, which requires bridging via EMPTY_MODEL→DEPLOYED,
          causing _inject_bridge to speculatively label 'deploy'.  That branch turns out to be a
          dead-end (no path back to EMPTY_MODEL), so it's abandoned.
        * The correct branch via EMPTY_MODEL runs 'deploy' as a user-selected destination —
          it must NOT carry the [injected] label from the discarded branch.
        """
        bridge_to_empty = make_item("test_bridge_to_empty")  # NO_CONTROLLER → EMPTY_MODEL
        deploy = make_item("test_deploy")  # EMPTY_MODEL → DEPLOYED  (user-selected)
        teardown = make_item("test_teardown")  # DEPLOYED → NEIGHBOR_ONLY (user-selected)
        redeploy = make_item("test_redeploy")  # NEIGHBOR_ONLY → DEPLOYED (user-selected)

        graph, all_transitions = _graph_and_all(
            (State.NO_CONTROLLER, State.EMPTY_MODEL, bridge_to_empty),
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
            (State.DEPLOYED, State.NEIGHBOR_ONLY, teardown),
            (State.NEIGHBOR_ONLY, State.DEPLOYED, redeploy),
        )

        # WHEN deploy, teardown, and redeploy are all user-selected
        plan = _build_execution_plan(
            current_state=State.NO_CONTROLLER,
            pure_clusters=defaultdict(list),
            selected_transitions=defaultdict(
                list,
                {
                    StateTransition(State.EMPTY_MODEL, State.DEPLOYED): [deploy],
                    StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY): [teardown],
                    StateTransition(State.NEIGHBOR_ONLY, State.DEPLOYED): [redeploy],
                },
            ),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN deploy is in the plan
        assert deploy in plan

        # AND deploy is NOT marked injected (it was user-selected, not a bridge)
        assert deploy.get_closest_marker("injected") is None
        assert not deploy.name.startswith("[injected]")

        # AND only the true bridge is marked injected
        assert bridge_to_empty.get_closest_marker("injected") is not None

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

    def test_repeated_bridge_item_gets_unique_nodeid_per_occurrence(
        self, make_item: Callable[..., pytest.Item]
    ) -> None:
        """Regression (SQT-913 / GH-445): a test scheduled more than once must not
        report each run under the same nodeid, or JUnit consumers (Test Observer)
        compact the separate runs into a single test case, hiding one of them.

        ``teardown`` is user-selected to reach NEIGHBOR_ONLY, then consumed by
        ``test_c``.  A second selected transition (``test_d``) still needs to
        depart NEIGHBOR_ONLY, but the only way back there is via
        ``old_revision -> deployed -> neighbor_only``, which re-crosses the
        ``teardown`` edge as a plain bridge (its selected occurrence is already
        scheduled) - injecting the exact same ``teardown`` Item object a second
        time.
        """
        # GIVEN a graph where the only path back to NEIGHBOR_ONLY re-uses `teardown`
        teardown = make_item("test_teardown")  # DEPLOYED -> NEIGHBOR_ONLY (the only candidate)
        test_c = make_item("test_c")  # NEIGHBOR_ONLY -> DEPLOYED_WITH_OLD_REVISION
        test_d = make_item("test_d")  # NEIGHBOR_ONLY -> DEPLOYED_WITH_UPGRADED_CONTROLLER
        back_from_c = make_item("test_back_from_c")  # DEPLOYED_WITH_OLD_REVISION -> DEPLOYED

        graph, all_transitions = _graph_and_all(
            (State.DEPLOYED, State.NEIGHBOR_ONLY, teardown),
            (State.NEIGHBOR_ONLY, State.DEPLOYED_WITH_OLD_REVISION, test_c),
            (State.NEIGHBOR_ONLY, State.DEPLOYED_WITH_UPGRADED_CONTROLLER, test_d),
            (State.DEPLOYED_WITH_OLD_REVISION, State.DEPLOYED, back_from_c),
        )

        # WHEN teardown, test_c, and test_d are all user-selected
        plan = _build_execution_plan(
            current_state=State.DEPLOYED,
            pure_clusters=defaultdict(list),
            selected_transitions=defaultdict(
                list,
                {
                    StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY): [teardown],
                    StateTransition(State.NEIGHBOR_ONLY, State.DEPLOYED_WITH_OLD_REVISION): [test_c],
                    StateTransition(State.NEIGHBOR_ONLY, State.DEPLOYED_WITH_UPGRADED_CONTROLLER): [test_d],
                },
            ),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN teardown is scheduled twice: once as the selected transition, once
        # re-injected as a bridge to reach NEIGHBOR_ONLY again for test_d.
        teardown_occurrences = [item for item in plan if "test_teardown" in item.name]
        assert len(teardown_occurrences) == 2

        # AND each occurrence has a distinct nodeid, so JUnit reports two separate
        # test cases instead of compacting them into one.
        assert len({item.nodeid for item in teardown_occurrences}) == 2

        # AND the first occurrence is the original item, untouched in identity
        first, second = teardown_occurrences
        assert first is teardown

        # AND the second occurrence is an independent object, not an alias of the first
        assert second is not teardown
        assert second is not first

    def test_unconnected_nodes_raises_unreachable_state_error(self, make_item: Callable[..., pytest.Item]) -> None:
        """Unconnected nodes: a state exists in the graph but is unreachable from current state.

        The graph has two disconnected components: EMPTY_MODEL → DEPLOYED and
        NO_CONTROLLER → NEIGHBOR_ONLY. Starting from EMPTY_MODEL, the algorithm
        detects that NO_CONTROLLER and NEIGHBOR_ONLY are unreachable and warns.
        If a test requires an unreachable state, it raises _UnreachableStateError.
        """
        # GIVEN a graph with two disconnected components
        deploy = make_item("test_deploy")  # EMPTY_MODEL → DEPLOYED
        isolate_switch = make_item("test_isolate_switch")  # NO_CONTROLLER → NEIGHBOR_ONLY
        unreachable_pure = make_item("test_unreachable")

        graph, all_transitions = _graph_and_all(
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
            # Isolated component: unreachable from EMPTY_MODEL
            (State.NO_CONTROLLER, State.NEIGHBOR_ONLY, isolate_switch),
        )

        # WHEN the plan is built and a test requiring the unreachable NEIGHBOR_ONLY is selected
        # THEN _UnreachableStateError is raised
        with pytest.raises(_UnreachableStateError, match="No path from state"):
            _build_execution_plan(
                current_state=State.EMPTY_MODEL,
                pure_clusters={State.NEIGHBOR_ONLY: [unreachable_pure]},
                selected_transitions=defaultdict(list),
                all_transitions=all_transitions,
                full_graph=graph,
            )

    def test_two_non_cyclical_destinations(self, make_item: Callable[..., pytest.Item]) -> None:
        """Two separate non-cyclical destinations with independent paths and backtracking.

        The scheduler must:
        1. Navigate from EMPTY_MODEL → DEPLOYED (run first dest, say teardown_1)
        2. Backtrack via redeploy bridge: NEIGHBOR_ONLY → DEPLOYED
        3. Navigate to second destination: DEPLOYED → NO_CONTROLLER (run teardown_2)

        This tests the memoization and cycle-detection logic ensures no infinite loops
        when backtracking between diverging branches.
        """
        # GIVEN a graph with two separate teardown transitions requiring backtracking
        deploy = make_item("test_deploy")
        teardown_1 = make_item("test_teardown_1")  # DEPLOYED → NEIGHBOR_ONLY
        teardown_2 = make_item("test_teardown_2")  # DEPLOYED → NO_CONTROLLER
        redeploy = make_item("test_redeploy")  # NEIGHBOR_ONLY → DEPLOYED

        graph, all_transitions = _graph_and_all(
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
            (State.DEPLOYED, State.NEIGHBOR_ONLY, teardown_1),
            (State.DEPLOYED, State.NO_CONTROLLER, teardown_2),
            (State.NEIGHBOR_ONLY, State.DEPLOYED, redeploy),
            # NO_CONTROLLER has no return path; ensures asymmetry
        )

        # WHEN both teardowns are selected (non-cyclical destinations)
        plan = _build_execution_plan(
            current_state=State.EMPTY_MODEL,
            pure_clusters=defaultdict(list),
            selected_transitions=defaultdict(
                list,
                {
                    StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY): [teardown_1],
                    StateTransition(State.DEPLOYED, State.NO_CONTROLLER): [teardown_2],
                },
            ),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN both teardowns are in the plan
        assert teardown_1 in plan
        assert teardown_2 in plan

        # AND the redeploy bridge is injected between them to re-navigate
        assert redeploy in plan
        assert plan.index(teardown_1) < plan.index(redeploy)
        assert plan.index(redeploy) < plan.index(teardown_2)

        # AND deploy is injected at the start to reach DEPLOYED from EMPTY_MODEL
        assert deploy in plan
        assert plan.index(deploy) < plan.index(teardown_1)

    def test_zero_non_cyclical_nodes_cyclic_graph(self, make_item: Callable[..., pytest.Item]) -> None:
        """All paths involve cycles: memoization and cycle detection prevent infinite loops.

        A cyclic graph: DEPLOYED ↔ NEIGHBOR_ONLY. The scheduler must navigate to
        both destinations but must not infinitely loop. Memoization (state, remaining_destinations)
        and visiting set ensure termination.
        """
        # GIVEN a cyclic graph where two states form a loop
        deploy = make_item("test_deploy")
        forward = make_item("test_forward")  # DEPLOYED → NEIGHBOR_ONLY
        backward = make_item("test_backward")  # NEIGHBOR_ONLY → DEPLOYED
        pure_1 = make_item("test_pure_1")
        pure_2 = make_item("test_pure_2")

        graph, all_transitions = _graph_and_all(
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
            (State.DEPLOYED, State.NEIGHBOR_ONLY, forward),
            (State.NEIGHBOR_ONLY, State.DEPLOYED, backward),
        )

        # WHEN two pure tests require the two cyclic states
        plan = _build_execution_plan(
            current_state=State.EMPTY_MODEL,
            pure_clusters={
                State.DEPLOYED: [pure_1],
                State.NEIGHBOR_ONLY: [pure_2],
            },
            selected_transitions=defaultdict(list),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN both pure tests are in the plan (no infinite loop, memoization works)
        assert pure_1 in plan
        assert pure_2 in plan

        # AND bridges are injected to navigate: at least deploy is needed to reach DEPLOYED
        assert deploy in plan  # EMPTY_MODEL → DEPLOYED

        # AND at least one of the cycle transitions is used (forward or backward or both)
        # The scheduler will use the minimal path needed to reach both states
        assert (forward in plan) ^ (backward in plan), "At least one cycle transition should be in plan"

        # AND the order respects state machine: deploy at start, then states reached in sequence
        assert plan.index(deploy) < plan.index(pure_1)

    def test_cycle_with_unreachable_destination_raises(self, make_item: Callable[..., pytest.Item]) -> None:
        """A reachable loop must not hide an unreachable destination.

        Graph has a cycle (DEPLOYED <-> NEIGHBOR_ONLY), but NO_CONTROLLER is
        disconnected. The planner must terminate and raise _UnreachableStateError.
        """
        deploy = make_item("test_deploy")
        forward = make_item("test_forward")
        backward = make_item("test_backward")
        unreachable_pure = make_item("test_unreachable")

        graph, all_transitions = _graph_and_all(
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
            (State.DEPLOYED, State.NEIGHBOR_ONLY, forward),
            (State.NEIGHBOR_ONLY, State.DEPLOYED, backward),
        )

        with pytest.raises(_UnreachableStateError, match="No path from state"):
            _build_execution_plan(
                current_state=State.EMPTY_MODEL,
                pure_clusters={State.NO_CONTROLLER: [unreachable_pure]},
                selected_transitions=defaultdict(list),
                all_transitions=all_transitions,
                full_graph=graph,
            )

    def test_cycle_with_many_pure_clusters(self, make_item: Callable[..., pytest.Item]) -> None:
        """There should be a valid plan that reaches all pure clusters without infinite loops, even with many states and cycles."""
        deploy = make_item("test_deploy")
        forward = make_item("test_forward")
        backward = make_item("test_backward")
        kill_controller = make_item("test_kill_controller")
        pure_1 = make_item("test_pure_1")
        pure_2 = make_item("test_pure_2")
        pure_3 = make_item("test_pure_3")

        graph, all_transitions = _graph_and_all(
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
            (State.DEPLOYED, State.NEIGHBOR_ONLY, forward),
            (State.NEIGHBOR_ONLY, State.DEPLOYED, backward),
        )

        plan = _build_execution_plan(
            current_state=State.EMPTY_MODEL,
            pure_clusters={
                State.DEPLOYED: [pure_3],
                State.NEIGHBOR_ONLY: [pure_2],
                State.NO_CONTROLLER: [pure_1],
            },
            selected_transitions=defaultdict(
                list,
                {
                    StateTransition(State.DEPLOYED, State.NO_CONTROLLER): [kill_controller],
                },
            ),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        assert pure_1 in plan
        assert pure_2 in plan
        assert pure_3 in plan
        assert deploy in plan
        assert kill_controller in plan
        assert plan.index(pure_1) > plan.index(pure_3)
        assert plan.index(pure_1) > plan.index(pure_2)

    def test_deployed_with_old_revision_cycle_succeeds(self, make_item: Callable[..., pytest.Item]) -> None:
        """Regression: the full suite graph with DEPLOYED_WITH_OLD_REVISION must produce a valid plan.

        The real test suite has a cycle through a third state:
            DEPLOYED → DEPLOYED_WITH_OLD_REVISION (downgrade)
            DEPLOYED_WITH_OLD_REVISION → DEPLOYED (upgrade)
            DEPLOYED → NEIGHBOR_ONLY (teardown)
            NEIGHBOR_ONLY → DEPLOYED (idempotent redeploy)
            NEIGHBOR_ONLY → DEPLOYED_WITH_OLD_REVISION (deploy old revision)

        With pure tests at DEPLOYED, transitions on every edge, and the starting
        state far from any destination (NO_CONTROLLER), the scheduler must bridge
        the full path and visit every destination. A previous bug in _inject_bridge
        called _mark_as_injected during speculative backtracking, which mutated
        item._nodeid and corrupted set-based scheduled tracking (hash changed),
        causing the planner to believe items were never scheduled and declare all
        branches dead ends.
        """
        # GIVEN the full suite graph with upgrade/downgrade cycle
        bootstrap = make_item("test_bootstrap")  # NO_CONTROLLER → EMPTY_MODEL
        deploy = make_item("test_deploy")  # EMPTY_MODEL → DEPLOYED
        downgrade = make_item("test_downgrade")  # DEPLOYED → DEPLOYED_WITH_OLD_REVISION
        upgrade = make_item("test_upgrade")  # DEPLOYED_WITH_OLD_REVISION → DEPLOYED
        teardown = make_item("test_teardown")  # DEPLOYED → NEIGHBOR_ONLY
        redeploy = make_item("test_redeploy")  # NEIGHBOR_ONLY → DEPLOYED
        deploy_old = make_item("test_deploy_old")  # NEIGHBOR_ONLY → DEPLOYED_WITH_OLD_REVISION
        pure_restart = make_item("test_restart")  # pure at DEPLOYED
        pure_pod = make_item("test_pod_deletion")  # pure at DEPLOYED

        graph, all_transitions = _graph_and_all(
            (State.NO_CONTROLLER, State.EMPTY_MODEL, bootstrap),
            (State.EMPTY_MODEL, State.DEPLOYED, deploy),
            (State.DEPLOYED, State.DEPLOYED_WITH_OLD_REVISION, downgrade),
            (State.DEPLOYED_WITH_OLD_REVISION, State.DEPLOYED, upgrade),
            (State.DEPLOYED, State.NEIGHBOR_ONLY, teardown),
            (State.NEIGHBOR_ONLY, State.DEPLOYED, redeploy),
            (State.NEIGHBOR_ONLY, State.DEPLOYED_WITH_OLD_REVISION, deploy_old),
        )

        # WHEN all non-bridge tests are user-selected
        plan = _build_execution_plan(
            current_state=State.NO_CONTROLLER,
            pure_clusters={State.DEPLOYED: [pure_restart, pure_pod]},
            selected_transitions=defaultdict(
                list,
                {
                    StateTransition(State.EMPTY_MODEL, State.DEPLOYED): [deploy],
                    StateTransition(State.DEPLOYED, State.DEPLOYED_WITH_OLD_REVISION): [downgrade],
                    StateTransition(State.DEPLOYED_WITH_OLD_REVISION, State.DEPLOYED): [upgrade],
                    StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY): [teardown],
                    StateTransition(State.NEIGHBOR_ONLY, State.DEPLOYED): [redeploy],
                    StateTransition(State.NEIGHBOR_ONLY, State.DEPLOYED_WITH_OLD_REVISION): [deploy_old],
                },
            ),
            all_transitions=all_transitions,
            full_graph=graph,
        )

        # THEN every user-selected item appears in the plan
        assert deploy in plan
        assert downgrade in plan
        assert upgrade in plan
        assert teardown in plan
        assert redeploy in plan
        assert deploy_old in plan
        assert pure_restart in plan
        assert pure_pod in plan

        # AND pure tests at DEPLOYED run before any transition out of DEPLOYED
        first_transition_from_deployed = min(plan.index(downgrade), plan.index(teardown))
        assert plan.index(pure_restart) < first_transition_from_deployed
        assert plan.index(pure_pod) < first_transition_from_deployed

        # AND the bootstrap bridge is injected (user didn't select it)
        assert bootstrap in plan
        assert bootstrap.get_closest_marker("injected") is not None

        # AND user-selected transitions that are never re-used as bridges are NOT marked injected
        assert deploy.get_closest_marker("injected") is None
        assert downgrade.get_closest_marker("injected") is None
        assert upgrade.get_closest_marker("injected") is None


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
