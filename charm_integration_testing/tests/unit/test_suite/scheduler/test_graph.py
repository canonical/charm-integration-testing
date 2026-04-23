# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for scheduler/graph.py - StateTransition and StateGraph."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from test_suite.scheduler.graph import StateGraph, StateTransition
from test_suite.scheduler.states import State

# Sentinel item used wherever a real pytest.Item is unnecessary.
_ITEM = object()


class TestStateTransition:
    def test_default_cost_is_one(self) -> None:
        # GIVEN a transition with no explicit cost
        transition = StateTransition(from_state=State.EMPTY_MODEL, to_state=State.DEPLOYED)

        # WHEN the cost attribute is read
        cost = transition.cost

        # THEN it defaults to 1
        assert cost == 1

    def test_explicit_cost_is_preserved(self) -> None:
        # GIVEN a transition with an explicit cost
        transition = StateTransition(from_state=State.EMPTY_MODEL, to_state=State.DEPLOYED, cost=5)

        # WHEN the cost attribute is read
        # THEN the supplied value is returned
        assert transition.cost == 5

    class TestLt:
        """StateTransition.__lt__ must exist so heapq can break ties."""

        @dataclass
        class Params:
            label: str
            a: StateTransition
            b: StateTransition
            expected: bool

        test_cases = [
            Params(
                label="less_when_from_state_sorts_earlier",
                a=StateTransition(State.DEPLOYED, State.EMPTY_MODEL),
                b=StateTransition(State.NEIGHBOR_ONLY, State.EMPTY_MODEL),
                expected=True,
            ),
            Params(
                label="less_when_from_equal_and_to_sorts_earlier",
                a=StateTransition(State.DEPLOYED, State.EMPTY_MODEL),
                b=StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY),
                expected=True,
            ),
            Params(
                label="not_less_when_from_state_sorts_later",
                a=StateTransition(State.NEIGHBOR_ONLY, State.DEPLOYED),
                b=StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY),
                expected=False,
            ),
            Params(
                label="not_less_when_equal",
                a=StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY),
                b=StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY),
                expected=False,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN two StateTransition objects
            # WHEN compared with <
            result = params.a < params.b

            # THEN the result matches the expected ordering
            assert result == params.expected


class TestStateGraph:
    class TestRegisterTransition:
        def test_source_and_target_become_known(self) -> None:
            # GIVEN an empty graph
            graph = StateGraph()
            transition = StateTransition(State.EMPTY_MODEL, State.DEPLOYED)

            # WHEN a transition is registered
            graph.register_transition(transition, _ITEM)

            # THEN both states are known
            assert State.EMPTY_MODEL in graph.known_states()
            assert State.DEPLOYED in graph.known_states()

        def test_multiple_items_on_same_edge(self) -> None:
            # GIVEN a graph with two different items registered on the same edge
            graph = StateGraph()
            t = StateTransition(State.EMPTY_MODEL, State.DEPLOYED)
            item_a, item_b = object(), object()
            graph.register_transition(t, item_a)
            graph.register_transition(t, item_b)

            # WHEN the shortest path is queried
            path = graph.shortest_path(State.EMPTY_MODEL, State.DEPLOYED)

            # THEN the path has exactly one hop (the edge is traversed once)
            assert path is not None
            assert len(path) == 1

        def test_multiple_outgoing_edges_from_one_state(self) -> None:
            # GIVEN a state with two outgoing edges
            graph = StateGraph()
            graph.register_transition(StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY), _ITEM)
            graph.register_transition(StateTransition(State.DEPLOYED, State.EMPTY_MODEL), _ITEM)

            # WHEN known_states is called
            # THEN all three states are present
            assert State.DEPLOYED in graph.known_states()
            assert State.NEIGHBOR_ONLY in graph.known_states()
            assert State.EMPTY_MODEL in graph.known_states()

    class TestKnownStates:
        def test_empty_graph_returns_empty_frozenset(self) -> None:
            # GIVEN an empty graph
            graph = StateGraph()

            # WHEN known_states is called
            result = graph.known_states()

            # THEN it is empty
            assert result == frozenset()

        def test_all_states_across_multiple_edges(self) -> None:
            # GIVEN a graph with three transitions covering four states
            graph = StateGraph()
            for from_s, to_s in [
                (State.NO_CONTROLLER, State.EMPTY_MODEL),
                (State.EMPTY_MODEL, State.DEPLOYED),
                (State.DEPLOYED, State.NEIGHBOR_ONLY),
            ]:
                graph.register_transition(StateTransition(from_s, to_s), _ITEM)

            # WHEN known_states is called
            result = graph.known_states()

            # THEN all four states are present
            assert result == frozenset({State.NO_CONTROLLER, State.EMPTY_MODEL, State.DEPLOYED, State.NEIGHBOR_ONLY})

    class TestShortestPath:
        @dataclass
        class Params:
            label: str
            edges: list[tuple[State, State, int]]
            from_state: State
            to_state: State
            expected_hops: int | None
            """Number of edges in the shortest path, or None if unreachable."""

        test_cases = [
            Params(
                label="same_state_returns_empty_list",
                edges=[],
                from_state=State.DEPLOYED,
                to_state=State.DEPLOYED,
                expected_hops=0,
            ),
            Params(
                label="direct_single_hop",
                edges=[(State.EMPTY_MODEL, State.DEPLOYED, 1)],
                from_state=State.EMPTY_MODEL,
                to_state=State.DEPLOYED,
                expected_hops=1,
            ),
            Params(
                label="two_hop_path",
                edges=[
                    (State.EMPTY_MODEL, State.DEPLOYED, 1),
                    (State.DEPLOYED, State.NEIGHBOR_ONLY, 1),
                ],
                from_state=State.EMPTY_MODEL,
                to_state=State.NEIGHBOR_ONLY,
                expected_hops=2,
            ),
            Params(
                label="full_chain_three_hops",
                edges=[
                    (State.NO_CONTROLLER, State.EMPTY_MODEL, 1),
                    (State.EMPTY_MODEL, State.DEPLOYED, 1),
                    (State.DEPLOYED, State.NEIGHBOR_ONLY, 1),
                ],
                from_state=State.NO_CONTROLLER,
                to_state=State.NEIGHBOR_ONLY,
                expected_hops=3,
            ),
            Params(
                label="unreachable_returns_none",
                edges=[(State.EMPTY_MODEL, State.DEPLOYED, 1)],
                from_state=State.EMPTY_MODEL,
                to_state=State.NEIGHBOR_ONLY,
                expected_hops=None,
            ),
            Params(
                label="no_path_when_graph_is_empty",
                edges=[],
                from_state=State.EMPTY_MODEL,
                to_state=State.DEPLOYED,
                expected_hops=None,
            ),
            Params(
                label="prefers_direct_over_longer_path",
                edges=[
                    # Direct path: cost 1
                    (State.EMPTY_MODEL, State.DEPLOYED, 1),
                    # Longer path via NEIGHBOR_ONLY: cost 2
                    (State.EMPTY_MODEL, State.NEIGHBOR_ONLY, 1),
                    (State.NEIGHBOR_ONLY, State.DEPLOYED, 1),
                ],
                from_state=State.EMPTY_MODEL,
                to_state=State.DEPLOYED,
                expected_hops=1,
            ),
            Params(
                label="prefers_lower_cost_edge",
                edges=[
                    # Expensive direct edge
                    (State.EMPTY_MODEL, State.DEPLOYED, 10),
                    # Cheaper two-hop route
                    (State.EMPTY_MODEL, State.NEIGHBOR_ONLY, 1),
                    (State.NEIGHBOR_ONLY, State.DEPLOYED, 1),
                ],
                from_state=State.EMPTY_MODEL,
                to_state=State.DEPLOYED,
                expected_hops=2,
            ),
            Params(
                label="cyclic_graph_does_not_loop_forever",
                edges=[
                    (State.DEPLOYED, State.NEIGHBOR_ONLY, 1),
                    (State.NEIGHBOR_ONLY, State.DEPLOYED, 1),
                ],
                from_state=State.DEPLOYED,
                to_state=State.EMPTY_MODEL,
                expected_hops=None,
            ),
            Params(
                label="multiple_equal_cost_paths_with_tail_returns_valid_shortest_path",
                edges=[
                    # Diamond with tail: two equal-cost paths converge, then continue to destination
                    (State.EMPTY_MODEL, State.DEPLOYED, 1),
                    (State.EMPTY_MODEL, State.NO_CONTROLLER, 1),
                    (State.DEPLOYED, State.NEIGHBOR_ONLY, 1),
                    (State.NO_CONTROLLER, State.NEIGHBOR_ONLY, 1),
                    (State.NEIGHBOR_ONLY, State.DEPLOYED_WITH_OLD_REVISION, 1),
                ],
                from_state=State.EMPTY_MODEL,
                to_state=State.DEPLOYED_WITH_OLD_REVISION,
                expected_hops=3,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN a graph built from the specified edges
            graph = StateGraph()
            for from_s, to_s, cost in params.edges:
                graph.register_transition(StateTransition(from_s, to_s, cost), _ITEM)

            # WHEN the shortest path is queried
            path = graph.shortest_path(params.from_state, params.to_state)

            # THEN the path length matches the expected number of hops
            if params.expected_hops is None:
                assert path is None
            else:
                assert path is not None
                assert len(path) == params.expected_hops

        def test_returned_path_contains_correct_transitions(self) -> None:
            # GIVEN a two-hop graph
            graph = StateGraph()
            t1 = StateTransition(State.EMPTY_MODEL, State.DEPLOYED)
            t2 = StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY)
            graph.register_transition(t1, _ITEM)
            graph.register_transition(t2, _ITEM)

            # WHEN the path from EMPTY_MODEL to NEIGHBOR_ONLY is queried
            path = graph.shortest_path(State.EMPTY_MODEL, State.NEIGHBOR_ONLY)

            # THEN the transitions in the path match the registered edges in order
            assert path is not None
            assert path[0][0] == t1
            assert path[1][0] == t2

        def test_returned_path_includes_registered_item(self) -> None:
            # GIVEN an edge with a specific sentinel item
            graph = StateGraph()
            sentinel = object()
            graph.register_transition(StateTransition(State.EMPTY_MODEL, State.DEPLOYED), sentinel)

            # WHEN the path is queried
            path = graph.shortest_path(State.EMPTY_MODEL, State.DEPLOYED)

            # THEN the item in the path tuple is the registered sentinel
            assert path is not None
            assert path[0][1] is sentinel

        def test_multiple_shortest_paths_returns_valid_path(self) -> None:
            # GIVEN a diamond graph with tail: two equal-cost paths converge then continue
            graph = StateGraph()
            t1 = StateTransition(State.EMPTY_MODEL, State.DEPLOYED, 1)
            t2 = StateTransition(State.EMPTY_MODEL, State.NO_CONTROLLER, 1)
            t3 = StateTransition(State.DEPLOYED, State.NEIGHBOR_ONLY, 1)
            t4 = StateTransition(State.NO_CONTROLLER, State.NEIGHBOR_ONLY, 1)
            t5 = StateTransition(State.NEIGHBOR_ONLY, State.DEPLOYED_WITH_OLD_REVISION, 1)
            graph.register_transition(t1, _ITEM)
            graph.register_transition(t2, _ITEM)
            graph.register_transition(t3, _ITEM)
            graph.register_transition(t4, _ITEM)
            graph.register_transition(t5, _ITEM)

            # WHEN the path from EMPTY_MODEL to DEPLOYED_WITH_OLD_REVISION is queried
            path = graph.shortest_path(State.EMPTY_MODEL, State.DEPLOYED_WITH_OLD_REVISION)

            # THEN a valid shortest path (of length 3) is returned with connected transitions
            assert path is not None
            assert len(path) == 3

            # Verify the path is valid: each transition's to_state must match the next's from_state
            transition1, _ = path[0]
            transition2, _ = path[1]
            transition3, _ = path[2]
            assert transition1.from_state == State.EMPTY_MODEL
            assert transition1.to_state == transition2.from_state
            assert transition2.to_state == transition3.from_state
            assert transition3.to_state == State.DEPLOYED_WITH_OLD_REVISION
