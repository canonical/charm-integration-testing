# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""State graph construction and shortest-path finding for the test scheduler.

The state graph is a directed weighted graph where nodes are canonical environment
states and edges are Transitions (test functions that move the environment from one
state to another). The scheduler uses Dijkstra's algorithm to find the cheapest
path between any two states.
"""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from typing import Any

from .states import State


@dataclass(frozen=True)
class StateTransition:
    """An edge in the state graph: a move from one environment state to another."""

    from_state: State
    to_state: State
    cost: int = 1

    def __lt__(self, other: StateTransition) -> bool:
        # Required for heapq comparisons when costs are equal.
        return (self.from_state, self.to_state) < (other.from_state, other.to_state)


@dataclass
class StateGraph:
    """Directed weighted graph of environment states connected by Transitions.

    Each edge stores both the :class:`StateTransition` metadata and the pytest item
    (the transition test) that realises the move.
    """

    _edges: dict[State, list[tuple[StateTransition, Any]]] = field(default_factory=dict, init=False, repr=False)

    def register_transition(self, transition: StateTransition, item: Any) -> None:
        """Add a transition edge, associating it with a pytest item."""
        self._edges.setdefault(transition.from_state, []).append((transition, item))

    def known_states(self) -> frozenset[State]:
        """Return all states that appear as either source or target of any edge."""
        states: set[State] = set()
        for from_state, edges in self._edges.items():
            states.add(from_state)
            for transition, _ in edges:
                states.add(transition.to_state)
        return frozenset(states)

    def shortest_path(self, from_state: State, to_state: State) -> list[tuple[StateTransition, Any]] | None:
        """Find the minimum-cost path from *from_state* to *to_state*.

        Uses Dijkstra's algorithm. Returns the ordered list of
        ``(transition, pytest_item)`` pairs along the path, or ``None`` if no
        path exists.  Returns an empty list when ``from_state == to_state``.

        The returned shortest path is randomly chosen among all minimum-cost paths, if there are multiple.
        """
        if from_state == to_state:
            return []

        # dist[state] -> cheapest total cost to reach that state
        dist: dict[State, int] = {from_state: 0}
        # parents[state] -> list of (transition, item) that serve as parents to this state in a shortest path
        parents: dict[State, list[tuple[StateTransition, Any]]] = {from_state: []}
        # heap entries: (cost, state)
        heap: list[tuple[int, State]] = [(0, from_state)]

        while heap:
            cost, state = heapq.heappop(heap)

            if state == to_state:
                # reconstruct the shortest path backwards using parents[state] from to_state until
                # we reach from_state, randomly choosing among multiple parents when they exist.
                # since all parents of a state have the same cost, we can pick any one
                path = [random.choice(parents[state])]  # nosec B311
                while (last_state := path[-1][0].from_state) != from_state:
                    path.append(random.choice(parents[last_state]))  # nosec B311
                return path[::-1]

            # Skip stale heap entries.
            if cost > dist.get(state, float("inf")):
                continue

            for transition, item in self._edges.get(state, []):
                new_cost = cost + transition.cost
                neighbor = transition.to_state
                found_dist = dist.get(neighbor, float("inf"))
                if new_cost <= found_dist:
                    dist[neighbor] = new_cost
                    if new_cost < found_dist:
                        heapq.heappush(heap, (new_cost, neighbor))
                        parents[neighbor] = []
                    parents[neighbor].append((transition, item))

        return None  # to_state is unreachable from from_state
