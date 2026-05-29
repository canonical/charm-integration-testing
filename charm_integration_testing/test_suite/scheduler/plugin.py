# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""pytest plugin: state-driven, graph-optimised test scheduler.

This plugin implements ``pytest_collection_modifyitems`` to reorder the
user's selected tests and automatically inject any bridging transition tests
needed to reach the required states.

testmon integration: the core/leaf model
-----------------------------------------
This suite is a *linear dependency chain* against live infrastructure, not a
set of independent tests.  Running a downstream test requires its upstream
tests to have run **in the same session** to build the live state (a
bootstrapped controller, a created model, a deployed bundle).  testmon's model
- "this file's coverage is unchanged, so skip it" - is the opposite
assumption: it treats tests as independent and freely deselectable.  Unchanged
*code* says nothing about whether the live *infrastructure* that an upstream
test produces is present.

We reconcile this by splitting the suite into two classes:

* **Core (spine) tests** - the mandatory base suite that must run every time
  to establish state.  Tagged ``@pytest.mark.core``.  These are NEVER subject
  to testmon deselection.
* **Leaf tests** - the optional extended tests.  testmon owns selection of
  these: if their code is unchanged, testmon deselects them and they don't
  run.  This is where testmon's speedup applies, and where it is real, since
  each leaf is an expensive operation against live infra.

The mechanism (see ``pytest_collection_modifyitems`` below) relies on hook
ordering.  A ``hookwrapper`` removes the core items from ``items`` *before*
testmon's own deselection runs, so testmon only ever sees leaves.  testmon
deselects among the leaves (clean collected/deselected/selected arithmetic,
no underflow), then the wrapper restores the full core set and runs the
scheduler on ``core + selected_leaves``.  Because core items never pass
through testmon's deselection they are never in its deselected tally, so
re-adding them cannot corrupt the count.

Run it as plain ``--testmon`` with NO ``-k``/``-m`` selection args::

    pytest --testmon --current-state no_bundle

Do NOT use ``--testmon-noselect`` (disables deselection entirely) and do NOT
combine ``--testmon`` with ``-k`` (which forces testmon into no-select mode).

Critically, the scheduler must NOT mutate item ``nodeid`` values, because
testmon keys its stored fingerprints on ``nodeid``; a mutated nodeid (e.g. an
``[injected]`` prefix) would never match testmon's database and the bridged
test would be re-selected forever.  Injected bridges are therefore labelled via
``user_properties`` and rendered through ``pytest_report_teststatus`` instead.

How it works
------------
0. ``pytest_ignore_collect`` (``tryfirst=True``) forces collection of every
   ``.py`` file inside the test-suite package, so the full state graph is
   available even when testmon would otherwise skip files.

1. ``pytest_itemcollected`` captures *every* test item as it is collected,
   before any filtering, into ``_all_collected``.  The full state graph is
   built from this.

2. ``pytest_collection_modifyitems`` (``hookwrapper``) hides core items from
   testmon, lets testmon deselect leaves during ``yield``, restores the core
   items, then runs the scheduler on the combined set.

3. The scheduler treats core tests and any testmon-selected leaves as
   destinations, and injects bridging transitions (from the full collection)
   needed to reach them - even bridges that testmon deselected.  Injected
   bridges do not affect testmon's count because they were never selected.
"""

from __future__ import annotations

import logging
import pathlib
from collections import defaultdict

import pytest

from .graph import StateGraph, StateTransition
from .markers import StateMarker, read_state_marker
from .states import State

logger = logging.getLogger(__name__)

# Absolute path to the test_suite package directory.
_TEST_SUITE_DIR = pathlib.Path(__file__).resolve().parent.parent

#: State assumed when no ``--current-state`` flag is given.
_DEFAULT_CURRENT_STATE = State.NO_BUNDLE

# Key used in item.user_properties to flag scheduler-injected bridges.
_INJECTED_PROP = "scheduler_injected"

# All items collected by pytest before any -k/-m filtering.
_all_collected: list[pytest.Item] = []

# Item object IDs already labelled as injected, so labelling is idempotent.
_injected_item_ids: set[int] = set()

# Set to the first state-marked item that fails at call-time.  Once non-None,
# all subsequent state-marked tests are skipped because the environment state
# is unknown. Pure test failures still set this: any state-marked failure
# leaves the environment indeterminate.
_failed_state_test: pytest.Item | None = None


# ---------------------------------------------------------------------------
# Plugin hooks
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``state``, ``injected`` and ``core`` markers."""
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
    config.addinivalue_line(
        "markers",
        (
            "core: Marks a test as part of the mandatory base (spine) suite.  "
            "Core tests always run and are never deselected by testmon: the "
            "scheduler hides them from testmon's deselection and restores them "
            "afterward.  Tag the base-suite chain (e.g. build_bundle, "
            "bootstrap_controller, create_model, deploy, scale, teardown) with "
            "this marker.  Everything else is a leaf that testmon may deselect "
            "when unchanged."
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
            "live model so the scheduler does not re-run expensive setup transitions. "
            f"Valid values: {valid_states}."
        ),
    )


def pytest_itemcollected(item: pytest.Item) -> None:
    """Record every item before -k/-m filtering so the full graph is available."""
    _all_collected.append(item)


@pytest.hookimpl(tryfirst=True)
def pytest_ignore_collect(collection_path: pathlib.Path, config: pytest.Config) -> bool | None:
    """Force collection of test-suite files so the state graph is complete.

    ``pytest_ignore_collect`` is a ``firstresult`` hook: the first non-None
    result wins.  Running ``tryfirst=True`` and returning ``False`` guarantees
    the file is collected before testmon's own ignore hook can veto it.

    Note: this only guarantees collection for files reachable by pytest's
    collection walk.  When testmon runs in its default selecting mode it may
    still skip collecting unchanged files at a layer this hook cannot reach.
    For a complete graph, run testmon in ``--testmon-noselect`` mode.
    """
    if collection_path.suffix == ".py" and collection_path.resolve().is_relative_to(_TEST_SUITE_DIR):
        return False
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:  # type: ignore[misc]
    """Detect failed state-marked tests and halt the state machine.

    When any state-marked test fails at setup, call, or teardown time the
    environment state is no longer known.  All subsequent state-marked tests
    are skipped to prevent them running against a broken environment.  Unmarked
    tests are never affected.
    """
    global _failed_state_test
    outcome = yield
    if _failed_state_test is not None:
        return
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


def pytest_report_teststatus(report: pytest.TestReport, config: pytest.Config) -> tuple[str, str, str] | None:
    """Render a visible '[injected]' label without mutating the nodeid.

    The nodeid must stay intact so testmon can key its stored fingerprints on
    it.  We instead surface the injected status through the report's word
    output during the call phase, leaving collection/setup/teardown untouched.
    """
    if report.when != "call":
        return None
    is_injected = any(name == _INJECTED_PROP and value for name, value in report.user_properties)
    if not is_injected:
        return None
    if report.passed:
        return "passed", ".", ("PASSED [injected]", {"green": True})
    if report.failed:
        return "failed", "F", ("FAILED [injected]", {"red": True})
    return None


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    """Reset module-level state so re-running pytest in the same process starts fresh."""
    global _all_collected, _injected_item_ids, _failed_state_test

    # With the core/leaf split the spine always runs, so a no-tests session
    # should not normally happen.  Guard anyway: if testmon deselected every
    # leaf and no core tests exist, treat the empty run as a successful no-op
    # rather than a failure.
    if (
        session.exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED
        and session.config.getoption("testmon", default=False)
        and _all_collected
    ):
        logger.info("No tests selected after testmon deselection; treating session as successful no-op.")
        session.exitstatus = pytest.ExitCode.OK

    _all_collected.clear()
    _injected_item_ids.clear()
    _failed_state_test = None


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip state-marked tests after a transition failure."""
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


def _is_core(item: pytest.Item) -> bool:
    """Return True if *item* is part of the mandatory base (spine) suite.

    Core tests are tagged ``@pytest.mark.core`` and must run on every session
    to establish live infrastructure state.  They are hidden from testmon so it
    can never deselect them.
    """
    return item.get_closest_marker("core") is not None


@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> object:
    """Hide core tests from testmon, then restore and schedule.

    With ``--testmon`` active this wrapper enforces the core/leaf split:

    1. Before ``yield``: pull the core (spine) items out of ``items`` so
       testmon's deselection - which runs during ``yield`` - only ever sees
       leaf tests.  This keeps testmon's collected/deselected/selected
       arithmetic consistent and prevents the negative-selected underflow that
       occurs when deselected items are re-added after the fact.
    2. During ``yield``: testmon (and any other ``modifyitems`` hooks) run,
       deselecting unchanged leaves.
    3. After ``yield``: ``items`` is now the testmon-selected leaves.  Restore
       the full core set in front of them and run the scheduler on the combined
       list to order everything into a valid state chain, injecting bridges as
       needed.

    Without ``--testmon`` there is nothing to hide; we simply yield and then
    schedule the full selection.
    """
    if not config.getoption("testmon", default=False):
        yield
        _schedule_items(config, items)
        return

    core_items = [it for it in items if _is_core(it)]
    leaf_items = [it for it in items if not _is_core(it)]

    # Hand testmon only the leaves; it deselects among these during yield.
    items[:] = leaf_items
    yield
    # items is now testmon-selected leaves. Restore core in front, then schedule.
    items[:] = core_items + items
    _schedule_items(config, items)


def _schedule_items(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Core scheduling logic: order items into a valid state chain.

    ``items`` arrives as the final run set (core + selected leaves).  The
    function reorders it in place so that state prerequisites are satisfied,
    injecting bridging transition tests from the full collection where needed.
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
    # ------------------------------------------------------------------
    full_graph = StateGraph()
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
    # 2. Partition the USER-SELECTED items into marked and unmarked.
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
    # 3. Build destination clusters from the selected items.
    # ------------------------------------------------------------------
    pure_clusters, selected_transitions = _partition_destinations(selected_marked)

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
        if not config.getoption("testmon", default=False):
            logger.error("Scheduler cannot build an execution plan: %s", exc)
            pytest.exit(str(exc), returncode=3)

        # Testmon deselected tests that broke the plan.  Rebuild using the full
        # suite to verify the graph itself is valid, and if so, use that plan.
        full_marked = _read_all_markers(_all_collected)
        full_pure, full_trans = _partition_destinations(full_marked)
        try:
            ordered = _build_execution_plan(
                current_state=current_state,
                pure_clusters=full_pure,
                selected_transitions=full_trans,
                all_transitions=all_transitions,
                full_graph=full_graph,
            )
        except _UnreachableStateError:
            logger.error(
                "Scheduler cannot build an execution plan even from the full suite: %s.  "
                "The state graph itself has no path to a required state - check that a "
                "transition test exists for the missing edge and that --current-state is "
                "correct.",
                exc,
            )
            pytest.exit(str(exc), returncode=3)

        logger.info("testmon deselected tests that the scheduler needs; " "rebuilt execution plan from the full suite.")

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

    Adds the ``injected`` marker and records a ``user_properties`` flag so the
    status can be rendered as ``[injected]`` by ``pytest_report_teststatus``.

    Crucially this does NOT mutate ``item.name`` or ``item._nodeid``: testmon
    keys its stored fingerprints on the nodeid, and a mutated nodeid would
    never match testmon's database, causing the bridge to be re-selected on
    every run.
    """
    if id(item) in _injected_item_ids:
        return
    _injected_item_ids.add(id(item))
    item.add_marker(pytest.mark.injected)
    # Surface the injected status without touching the nodeid.
    item.user_properties.append((_INJECTED_PROP, True))


def _read_all_markers(
    items: list[pytest.Item],
) -> list[tuple[pytest.Item, StateMarker]]:
    """Return ``(item, marker)`` pairs for every state-marked item, skipping unmarked ones."""
    result: list[tuple[pytest.Item, StateMarker]] = []
    for item in items:
        try:
            marker = read_state_marker(item)
        except ValueError:
            marker = None
        if marker is not None:
            result.append((item, marker))
    return result


def _partition_destinations(
    marked_items: list[tuple[pytest.Item, StateMarker]],
) -> tuple[dict[State, list[pytest.Item]], dict[StateTransition, list[pytest.Item]]]:
    """Split marked items into pure-state clusters and transition edges."""
    pure_clusters: dict[State, list[pytest.Item]] = defaultdict(list)
    selected_transitions: dict[StateTransition, list[pytest.Item]] = defaultdict(list)
    for item, marker in marked_items:
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
    return pure_clusters, selected_transitions


def _build_execution_plan(
    current_state: State,
    pure_clusters: dict[State, list[pytest.Item]],
    selected_transitions: dict[StateTransition, list[pytest.Item]],
    all_transitions: dict[StateTransition, list[pytest.Item]],
    full_graph: StateGraph,
) -> list[pytest.Item]:
    r"""Build an ordered item list using backtracking with memoization and cycle detection.

    See module docstring for the integration contract.  The algorithm uses
    exhaustive backtracking to reorder user-selected tests and inject bridging
    transitions, with dead-end memoization and an in-flight cycle guard to
    guarantee termination on cyclic graphs.

    Args:
        current_state: Environment state before any tests run.
        pure_clusters: Mapping from state to user-selected pure tests.
        selected_transitions: User-selected transition tests, keyed by edge.
        all_transitions: Every transition test in the full suite (bridging only).
        full_graph: State graph built from all collected transition tests.

    Returns:
        Ordered list of pytest items forming a valid execution plan.

    Raises:
        _UnreachableStateError: If no ordering of destinations bridges all gaps.
    """

    def _all_selected_at(s: State) -> list[pytest.Item]:
        """All user-selected items that depart from state *s*."""
        pure_tests = list(pure_clusters.get(s, []))
        transition_tests = [it for st, items in selected_transitions.items() if st.from_state == s for it in items]
        return pure_tests + transition_tests

    def _unscheduled_destinations(scheduled: set[pytest.Item]) -> set[State]:
        """Compute destination states that still have unscheduled items."""
        all_destinations: set[State] = set(pure_clusters.keys())
        for st in selected_transitions:
            all_destinations.add(st.from_state)
        return {s for s in all_destinations if any(it not in scheduled for it in _all_selected_at(s))}

    def _run_selected_at(s: State, plan: list[pytest.Item], scheduled: set[pytest.Item]) -> State:
        """Schedule all unscheduled pure tests at state *s*, then one transition."""
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

    def _inject_bridge(
        path: list[tuple[StateTransition, pytest.Item]],
        plan: list[pytest.Item],
        scheduled: set[pytest.Item],
        injected_ids: set[int],
    ) -> None:
        """Inject one bridging transition item per edge along *path*."""
        for transition, _graph_item in path:
            selected = selected_transitions.get(transition)
            unscheduled = next((it for it in selected if it not in scheduled), None) if selected else None
            if unscheduled is not None:
                plan.append(unscheduled)
                scheduled.add(unscheduled)
            else:
                candidates = all_transitions.get(transition)
                if candidates:
                    bridge_item = candidates[0]
                    injected_ids.add(id(bridge_item))
                    plan.append(bridge_item)

    dead_end_memo: set[tuple[State, frozenset[State]]] = set()
    visiting: set[tuple[State, frozenset[State]]] = set()

    def _backtrack_search(
        current_state: State,
        current_plan: list[pytest.Item],
        scheduled: set[pytest.Item],
        injected_ids: set[int],
    ) -> tuple[list[pytest.Item], set[int]] | None:
        """Recursively search for a valid ordering of destinations using backtracking."""
        remaining_destinations = _unscheduled_destinations(scheduled)
        if not remaining_destinations:
            return current_plan, injected_ids

        memo_key = (current_state, frozenset(remaining_destinations))

        if memo_key in dead_end_memo:
            return None

        if memo_key in visiting:
            logger.debug(
                f"Cycle detected at state '{current_state}' with remaining "
                f"destinations {sorted(remaining_destinations)}. Returning None."
            )
            return None

        visiting.add(memo_key)

        try:
            for target_state in sorted(remaining_destinations):
                raw_path = full_graph.shortest_path(current_state, target_state)
                if raw_path is None:
                    continue

                branch_plan = current_plan[:]
                branch_scheduled = scheduled.copy()
                branch_injected = injected_ids.copy()

                _inject_bridge(raw_path, branch_plan, branch_scheduled, branch_injected)
                new_state = _run_selected_at(target_state, branch_plan, branch_scheduled)
                result = _backtrack_search(new_state, branch_plan, branch_scheduled, branch_injected)

                if result is not None:
                    return result

            dead_end_memo.add(memo_key)
            return None
        finally:
            visiting.discard(memo_key)

    # ------------------------------------------------------------------
    # Initialize: handle destinations reachable at the starting state for free.
    # ------------------------------------------------------------------
    plan: list[pytest.Item] = []
    scheduled: set[pytest.Item] = set()
    state = current_state

    remaining = _unscheduled_destinations(scheduled)
    if state in remaining:
        state = _run_selected_at(state, plan, scheduled)
        remaining = _unscheduled_destinations(scheduled)

    # ------------------------------------------------------------------
    # Use exhaustive backtracking to find a valid ordering of destinations.
    # ------------------------------------------------------------------
    if remaining:
        backtrack_result = _backtrack_search(state, plan, scheduled, set())
        if backtrack_result is None:
            raise _UnreachableStateError(
                f"No path from state '{state}' to any of the remaining required states "
                f"{sorted(remaining)}.  "
                "No valid ordering of destinations could bridge this gap.  "
                "Add a transition test for the missing edge or set --current-state "
                "to a state closer to the required one."
            )
        plan, final_injected_ids = backtrack_result
        # Apply injected labels only now, after the final plan is committed.
        for item in plan:
            if id(item) in final_injected_ids:
                _mark_as_injected(item)

    return plan
