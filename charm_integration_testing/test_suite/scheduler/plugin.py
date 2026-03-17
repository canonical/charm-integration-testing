# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""pytest plugin: state-driven, graph-optimised test scheduler.

This plugin implements ``pytest_collection_modifyitems`` to reorder the
user's selected tests and automatically inject any bridging transition tests
needed to reach the required states.

How it works
------------
1. ``pytest_itemcollected`` captures *every* test item as it is collected,
   before any ``-k`` / ``-m`` filtering is applied.  This gives the scheduler
   a complete view of all available transitions in the suite.

2. ``pytest_collection_modifyitems`` (``trylast=True``) runs after pytest's
   own deselection, so ``items`` contains only what the user explicitly
   selected.  The scheduler treats these as **destinations**: tests that
   must run, in an order that respects their ``requires`` states.

3. The **full** state graph is built from all items captured in step 1.
   This means Dijkstra can find bridging paths even when the transition
   tests that form those paths were filtered out by ``-k``.

4. For each destination, the scheduler uses Dijkstra to find the shortest
   path from the current state.  Any bridging transition tests along that
   path are injected into the plan automatically (re-added from the full
   collection even if ``-k`` excluded them).

5. Tests *without* the ``@pytest.mark.state`` marker are left in their
   original relative order and appended after all scheduled tests.

Example
-------
Running::

    pytest -k test_teardown --current-state empty_model

The scheduler sees:

* **Full graph** (from all collected items): ``empty_model → deployed``,
  ``deployed → neighbor_only``, ``neighbor_only → deployed``.
* **User selection** (``items``): ``[test_teardown]``  (requires ``deployed``)
* **Plan**: navigate ``empty_model → deployed`` (inject ``test_deploy``),
  then run ``test_teardown``.
* **Result**: ``[test_deploy, test_teardown]``
"""

from __future__ import annotations

import logging
from collections import defaultdict

import pytest

from .graph import StateGraph, StateTransition
from .markers import StateMarker, read_state_marker
from .states import State

logger = logging.getLogger(__name__)

#: State assumed when no ``--current-state`` flag is given.
_DEFAULT_CURRENT_STATE = State.NO_CONTROLLER

# All items collected by pytest before any -k/-m filtering.
# Populated by pytest_itemcollected; used by modifyitems to build the full graph.
_all_collected: list[pytest.Item] = []

# Tracks item object IDs that have already been labelled as injected, so that
# re-injecting the same bridge item a second time does not double-prefix its name.
_injected_item_ids: set[int] = set()

# Set to the first transition item that fails at call-time.  Once non-None,
# all subsequent state-marked tests are skipped because the environment state
# is unknown. Pure test failures do NOT set this: they leave the state intact.
_failed_state_test: pytest.Item | None = None


# ---------------------------------------------------------------------------
# Plugin hooks
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``state`` and ``injected`` markers."""
    config.addinivalue_line(
        "markers",
        (
            "state(requires, provides=None, bridge_only=False): "
            "Declare the environment state(s) required by a test and the state it leaves "
            "behind after a successful run.  "
            "'requires' may be a single State or a list of States (the scheduler registers "
            "a separate graph edge for each).  "
            "If 'provides' is omitted the test is assumed to leave the state unchanged "
            "(only valid when a single requires state is given).  "
            "Tests where provides is not in requires are *transition tests*: the scheduler "
            "may inject them automatically to bridge gaps between states.  "
            "Set bridge_only=True to mark a test as a helper that is never treated as a "
            "user-selected destination: it will only ever run as an injected bridge."
        ),
    )
    config.addinivalue_line(
        "markers",
        (
            "injected: Added automatically by the scheduler to bridging transition tests "
            "that were not explicitly requested by the user (e.g. via -k).  "
            "These tests are inserted to satisfy state prerequisites and may be "
            "excluded from the run with '-m \"not injected\"'."
        ),
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the ``--current-state`` CLI option consumed by the scheduler."""
    valid_states = ", ".join(f"'{s.value}'" for s in State)
    parser.addoption(
        "--current-state",
        type=str,
        default=_DEFAULT_CURRENT_STATE.value,
        help=(
            f"Current environment state before any tests run "
            f"(default: '{_DEFAULT_CURRENT_STATE.value}'). "
            "Use this when resuming a partial run or iterating locally against a "
            f"live model so the scheduler does not re-run expensive setup transitions. "
            f"Valid values: {valid_states}."
        ),
    )


def pytest_itemcollected(item: pytest.Item) -> None:
    """Record every item before -k/-m filtering so the full graph is available."""
    _all_collected.append(item)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:  # type: ignore[misc]
    """Detect failed state-marked tests and halt the state machine.

    When any state-marked test fails at setup, call, or teardown time the
    environment state is no longer known: a setup failure may leave the
    environment partially configured, and a teardown failure may leave it in
    an indeterminate state.  All subsequent state-marked tests are skipped to
    prevent them from running against a broken or indeterminate environment.

    Unmarked tests are never affected.
    """
    global _failed_state_test
    outcome = yield
    if _failed_state_test is not None:
        return  # Already halted; no need to re-check.
    report = outcome.get_result()
    if report.failed:
        try:
            marker = read_state_marker(item)
        except ValueError:
            marker = None
        if marker is not None:
            _failed_state_test = item
            logger.error(
                "State-marked test %r failed: environment state is unknown.  "
                "All remaining state-marked tests will be skipped.",
                item.nodeid,
            )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    """Reset module-level state so re-running pytest in the same process starts fresh.

    The three globals below are populated during a session and must be cleared
    when the session ends; otherwise a second ``pytest.main()`` call in the
    same Python process (e.g. from a test harness) would see stale data from
    the previous run.
    """
    global _all_collected, _injected_item_ids, _failed_state_test
    _all_collected.clear()
    _injected_item_ids.clear()
    _failed_state_test = None


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip state-marked tests after a transition failure.

    Called before each test's setup phase.  If a previous transition test
    has failed, this hook raises ``pytest.skip`` for every state-marked test
    that follows, leaving unmarked tests unaffected.
    """
    if _failed_state_test is None:
        return
    if item is _failed_state_test:
        return  # Don't skip the failing test itself; let it report naturally.
    try:
        marker = read_state_marker(item)
    except ValueError:
        marker = None
    if marker is not None:
        pytest.skip(f"Skipped: state-marked test {_failed_state_test.nodeid!r} failed: environment state is unknown.")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Reorder and augment the user's selected tests.

    ``items`` at this point contains only the user's ``-k``/``-m`` selection.
    The scheduler treats these as destinations, builds the full state graph
    from ``_all_collected``, and injects any bridging transitions needed to
    reach those destinations.
    """
    raw_state: str = config.getoption("--current-state")
    try:
        current_state = State(raw_state)
    except ValueError:
        valid = ", ".join(f"'{s.value}'" for s in State)
        pytest.exit(
            f"Invalid --current-state value '{raw_state}'. Valid values: {valid}",
            returncode=3,
        )

    # ------------------------------------------------------------------
    # 1. Build the full state graph from ALL collected items (pre-filter).
    #    This allows Dijkstra to find bridging paths even when the bridging
    #    transition tests were excluded by -k.
    # ------------------------------------------------------------------
    full_graph = StateGraph()
    # StateTransition -> [items], built from the complete collection.
    # Multiple tests may cover the same edge; all are recorded.
    all_transitions: dict[StateTransition, list[pytest.Item]] = defaultdict(list)

    for item in _all_collected:
        try:
            marker = read_state_marker(item)
        except ValueError as exc:
            pytest.exit(str(exc), returncode=3)
        if marker is not None and marker.is_transition:
            for req_state in marker.requires:
                t = StateTransition(from_state=req_state, to_state=marker.provides)
                full_graph.register_transition(t, item)
                all_transitions[t].append(item)

    # ------------------------------------------------------------------
    # 2. Partition the USER-SELECTED items (post -k filter) into marked
    #    and unmarked.  These are the destinations the scheduler must reach.
    # ------------------------------------------------------------------
    selected_marked: list[tuple[pytest.Item, StateMarker]] = []
    unmarked: list[pytest.Item] = []

    for item in items:
        try:
            marker = read_state_marker(item)
        except ValueError as exc:
            pytest.exit(str(exc), returncode=3)
        if marker is not None:
            selected_marked.append((item, marker))
        else:
            unmarked.append(item)

    if not selected_marked:
        # Nothing state-marked in the selection; leave items untouched.
        return

    # ------------------------------------------------------------------
    # 3. From the selected items, build destination clusters.
    #    - pure_clusters: state → [selected pure tests]
    #    - selected_transitions: StateTransition → [selected transition items]
    #      Multiple tests may share the same edge; all must run.
    # ------------------------------------------------------------------
    pure_clusters: dict[State, list[pytest.Item]] = defaultdict(list)
    selected_transitions: dict[StateTransition, list[pytest.Item]] = defaultdict(list)

    for item, marker in selected_marked:
        if marker.bridge_only:
            logger.debug(
                "Item %r is bridge_only: ignoring as a destination even though it was selected.",
                item.nodeid,
            )
            continue
        if marker.is_transition:
            for req_state in marker.requires:
                selected_transitions[StateTransition(from_state=req_state, to_state=marker.provides)].append(item)
        else:
            for req_state in marker.requires:
                pure_clusters[req_state].append(item)

    # ------------------------------------------------------------------
    # 4. Compute the ordered execution plan.
    # ------------------------------------------------------------------
    try:
        ordered = _build_execution_plan(
            current_state=current_state,
            pure_clusters=pure_clusters,
            selected_transitions=selected_transitions,
            all_transitions=all_transitions,
            full_graph=full_graph,
        )
    except _UnreachableStateError as exc:
        logger.error("Scheduler cannot build an execution plan: %s", exc)
        pytest.exit(str(exc), returncode=3)

    # ------------------------------------------------------------------
    # 5. Commit new order: scheduled items first, then any unmarked items.
    # ------------------------------------------------------------------
    items[:] = ordered + unmarked


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _UnreachableStateError(RuntimeError):
    """Raised when Dijkstra cannot find a path to a required state."""


def _mark_as_injected(item: pytest.Item) -> None:
    """Label *item* as a scheduler-injected bridge (idempotent).

    Adds the ``injected`` marker and prefixes the item's display name and
    node ID with ``[injected]`` so it is visually distinct in ``pytest -v``
    output.  Calling this function more than once on the same item is safe.
    """
    if id(item) in _injected_item_ids:
        return
    _injected_item_ids.add(id(item))
    item.add_marker(pytest.mark.injected)
    item.name = f"[injected] {item.name}"
    # pytest exposes no public API to override the node ID; _nodeid is the
    # backing attribute for the read-only ``nodeid`` property.  This is a
    # known limitation: revisit if pytest removes or renames _nodeid.
    item._nodeid = f"[injected] {item._nodeid}"


def _build_execution_plan(
    current_state: State,
    pure_clusters: dict[State, list[pytest.Item]],
    selected_transitions: dict[StateTransition, list[pytest.Item]],
    all_transitions: dict[StateTransition, list[pytest.Item]],
    full_graph: StateGraph,
) -> list[pytest.Item]:
    """Build a minimum-cost ordered item list.

    Starting from *current_state*, the algorithm greedily picks the nearest
    reachable destination state, injects the shortest-path bridging transitions
    needed to get there, then runs all user-selected items at that state.

    Destination states are states where at least one user-selected item departs
    (for transitions) or must run (for pure tests).

    Bridging transitions are drawn from *all_transitions* (the complete suite),
    so the scheduler can inject a transition that was filtered out by ``-k``
    when it is the only way to reach a selected test's required state.
    User-selected transition tests are always included in the plan directly;
    they are never silently skipped in favour of a purely-bridging injection.

    Multiple tests may share the same :class:`StateTransition` edge.  When
    bridging, only one representative item is injected (the first in the list).
    When the user has explicitly selected tests on an edge, all of them run.

    Args:
        current_state: Environment state before any tests run.
        pure_clusters: Mapping from state to user-selected pure tests that
            run inside that state without changing it.
        selected_transitions: User-selected transition tests, keyed by
            :class:`StateTransition`, with all items for that edge.
        all_transitions: Every transition test in the full suite, keyed by
            :class:`StateTransition`.  Used for bridging only.
        full_graph: State graph built from all collected transition tests.

    Returns:
        Ordered list of pytest items forming the optimal execution plan.

    Raises:
        _UnreachableStateError: If any destination state is unreachable from
            *current_state* even using the full suite of transitions.
    """
    plan: list[pytest.Item] = []
    scheduled: set[pytest.Item] = set()
    state = current_state

    def _all_selected_at(s: State) -> list[pytest.Item]:
        """All user-selected items that depart from state *s*."""
        pure_tests = list(pure_clusters.get(s, []))
        transition_tests = [it for st, items in selected_transitions.items() if st.from_state == s for it in items]
        return pure_tests + transition_tests

    def _unscheduled_destinations() -> set[State]:
        all_destinations: set[State] = set(pure_clusters.keys())
        for st in selected_transitions:
            all_destinations.add(st.from_state)
        return {s for s in all_destinations if any(it not in scheduled for it in _all_selected_at(s))}

    def _run_selected_at(s: State) -> State:
        """Schedule all unscheduled pure tests at state *s*, then one transition.

        Pure tests are appended first: they don't change state so all of them
        can run in one visit.  For transitions, only the first unscheduled item
        is run before returning.  This lets the outer loop re-navigate back to
        *s* (via a bridging redeploy, etc.) before running the next transition
        test on the same edge, ensuring each one starts from a freshly prepared
        environment.

        Returns the resulting state: unchanged if only pure tests ran, or the
        ``to_state`` of the transition that was executed.
        """
        for item in pure_clusters.get(s, []):
            if item not in scheduled:
                plan.append(item)
                scheduled.add(item)
        for st, items in list(selected_transitions.items()):
            if st.from_state == s:
                for item in items:
                    if item not in scheduled:
                        plan.append(item)
                        scheduled.add(item)
                        return st.to_state  # one transition at a time; re-navigate for the next
        return s

    def _inject_bridge(path: list[tuple[StateTransition, pytest.Item]]) -> None:
        """Inject one bridging transition item per edge along *path*.

        For each edge, if the user selected tests for it, prefer the first
        unscheduled one (so it counts as both bridge and selected destination).
        Otherwise use the first item from the full suite as a pure bridge;
        these are NOT added to ``scheduled``, so the same bridging test can be
        injected again if the scheduler needs to cross the same edge a second
        time (e.g. when two selected tests share an edge and each needs a
        fresh environment).
        """
        for transition, _graph_item in path:
            selected = selected_transitions.get(transition)
            unscheduled = next((it for it in selected if it not in scheduled), None) if selected else None
            if unscheduled is not None:
                # Prefer an unscheduled selected item; it will be added to
                # scheduled inside _run_selected_at when it executes.
                plan.append(unscheduled)
                scheduled.add(unscheduled)
            else:
                # No unscheduled selected item (either none exist, or all were
                # already pre-injected on a prior traversal of this edge).
                # Fall back to a pure bridge so the environment actually
                # transitions - silently skipping would leave it in the wrong state.
                candidates = all_transitions.get(transition)
                if candidates:
                    bridge_item = candidates[0]
                    _mark_as_injected(bridge_item)
                    plan.append(bridge_item)

    # Handle destinations reachable at the starting state for free.
    if state in _unscheduled_destinations():
        state = _run_selected_at(state)

    remaining = _unscheduled_destinations()
    while remaining:
        best_path: list[tuple[StateTransition, pytest.Item]] | None = None
        best_target: State | None = None
        best_cost: float = float("inf")

        for target_state in remaining:
            raw_path = full_graph.shortest_path(state, target_state)
            if raw_path is None:
                continue
            cost = sum(t.cost for t, _ in raw_path)
            if cost < best_cost:
                best_cost = cost
                best_path = raw_path
                best_target = target_state

        if best_path is None or best_target is None:
            raise _UnreachableStateError(
                f"No path from state '{state}' to any of the remaining required states "
                f"{sorted(remaining)}.  "
                "No transition tests exist in the suite to bridge this gap.  "
                "Add a transition test for the missing edge or set --current-state "
                "to a state closer to the required one."
            )

        _inject_bridge(best_path)
        state = best_target
        state = _run_selected_at(state)
        remaining = _unscheduled_destinations()

    return plan
