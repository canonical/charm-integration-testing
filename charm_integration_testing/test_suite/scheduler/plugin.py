# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""pytest plugin: state-driven, graph-optimised test scheduler.

This plugin implements ``pytest_collection_modifyitems`` to reorder the
user's selected tests and automatically inject any bridging transition tests
needed to reach the required states.

How it works
------------
0. ``pytest_ignore_collect`` (``tryfirst=True``) forces collection of every
   ``.py`` file inside the test-suite package.  This prevents pytest-testmon
   from skipping unchanged files entirely, which would starve the scheduler
   of the transition tests it needs to build the full state graph.

1. ``pytest_itemcollected`` captures *every* test item as it is collected,
   before any ``-k`` / ``-m`` filtering is applied.  This gives the scheduler
   a complete view of all available transitions in the suite.

2. ``pytest_collection_modifyitems`` (``trylast=True``) runs after pytest's
   own deselection (and testmon's deselection), so ``items`` contains only
   what the user explicitly selected.  The scheduler treats these as
   **destinations**: tests that must run, in an order that respects their
   ``requires`` states.

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
import pathlib
from collections import Counter, defaultdict

import pytest

from .graph import StateGraph, StateTransition
from .markers import StateMarker, read_state_marker
from .states import State

logger = logging.getLogger(__name__)

# Absolute path to the test_suite package directory.
# Used by pytest_ignore_collect to force-collect test files that testmon
# would otherwise skip entirely.
_TEST_SUITE_DIR = pathlib.Path(__file__).resolve().parent.parent

#: State assumed when no ``--current-state`` flag is given.
_DEFAULT_CURRENT_STATE = State.NO_BUNDLE

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

# Number of bridge items re-injected during collection-time scheduling.
# This is added to session.testscollected in pytest_collection_finish so
# pytest's final "collected / deselected / selected" bookkeeping stays
# consistent when bridges are re-added after testmon deselection.
_collection_reinjected_count = 0


def _state_test_files_relative_to_root(config: pytest.Config) -> set[str]:
    """Return test-suite files that declare scheduler state markers.

    testmon tracks deselected files by repository-relative path strings.
    This helper computes the same representation for every test file in the
    suite that contains a ``pytest.mark.state`` marker.
    """
    root = pathlib.Path(str(config.rootpath)).resolve()
    state_files: set[str] = set()
    for path in _TEST_SUITE_DIR.glob("test_*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "pytest.mark.state(" not in text:
            continue
        try:
            rel = path.resolve().relative_to(root)
        except ValueError:
            # Path lies outside the configured root (unexpected in this repo).
            continue
        state_files.add(rel.as_posix())
    return state_files


def _prune_testmon_deselected_state_files(config: pytest.Config) -> None:
    """Keep state-test files collectable even when testmon marks them stable.

    The scheduler requires these files to be collected so bridging transition
    tests are available for path-finding and injection.
    """
    testmon_selector = config.pluginmanager.get_plugin("TestmonSelect")
    if testmon_selector is None:
        return
    deselected_files = getattr(testmon_selector, "deselected_files", None)
    if not isinstance(deselected_files, list):
        return

    required_state_files = _state_test_files_relative_to_root(config)
    if not required_state_files:
        return

    before = len(deselected_files)
    testmon_selector.deselected_files = [path for path in deselected_files if path not in required_state_files]
    removed = before - len(testmon_selector.deselected_files)
    if removed:
        logger.info(
            "Scheduler kept %d state-test files from testmon file-level deselection.",
            removed,
        )


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
    config.addinivalue_line(
        "markers",
        (
            "core: Marks a test as part of the core state-transition chain.  "
            "Use with --testmon-forceselect -m 'core' to ensure these tests "
            "are always collected and selected even when testmon considers them unchanged."
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


def pytest_sessionstart(session: pytest.Session) -> None:
    """Ensure testmon does not suppress collection of scheduler-critical files."""
    _prune_testmon_deselected_state_files(session.config)


def pytest_itemcollected(item: pytest.Item) -> None:
    """Record every item before -k/-m filtering so the full graph is available."""
    _all_collected.append(item)


@pytest.hookimpl(tryfirst=True)
def pytest_ignore_collect(collection_path: pathlib.Path, config: pytest.Config) -> bool | None:
    """Force collection of the test-suite tree so the state graph is complete.

    pytest-testmon's ``pytest_ignore_collect`` skips unchanged files
    entirely, which prevents ``pytest_itemcollected`` from capturing
    them.  The state scheduler *requires* visibility of every transition
    test to build the full graph.

    Returning ``False`` with ``tryfirst=True`` short-circuits the
    ``firstresult`` hook chain and guarantees the items are collected.
    This must apply to the test-suite directory itself as well as leaf
    ``.py`` files, otherwise an ignore decision on a parent path can still
    prune unchanged transition tests before pytest ever visits the files.
    testmon can still deselect them during ``pytest_collection_modifyitems``;
    the scheduler will re-inject any that are needed as bridges.
    """
    resolved_path = collection_path.resolve()
    if resolved_path == _TEST_SUITE_DIR or (
        resolved_path.is_relative_to(_TEST_SUITE_DIR) and (resolved_path.is_dir() or resolved_path.suffix == ".py")
    ):
        return False
    return None


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
    global _all_collected, _injected_item_ids, _failed_state_test, _collection_reinjected_count

    # pytest-testmon may legitimately deselect every test in incremental CI
    # runs. In that case pytest returns NO_TESTS_COLLECTED (5), which should
    # be treated as a successful no-op run.
    #
    # Important: testmon may be auto-loaded when installed, even when this
    # run did not enable it. We only rewrite the exit status when testmon is
    # explicitly active for this invocation.
    if (
        session.exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED
        and _is_testmon_enabled(session.config)
        and _all_collected
    ):
        logger.info("No tests selected after testmon deselection; treating session as successful no-op.")
        session.exitstatus = pytest.ExitCode.OK

    _all_collected.clear()
    _injected_item_ids.clear()
    _failed_state_test = None
    _collection_reinjected_count = 0


def _is_testmon_enabled(config: pytest.Config) -> bool:
    """Return True when pytest-testmon is enabled for the current run."""
    try:
        return bool(config.getoption("testmon"))
    except (ValueError, AttributeError):
        return False


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
    global _collection_reinjected_count
    _collection_reinjected_count += _schedule_items(config, items)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Adjust collected-count bookkeeping for collection-time bridge injection."""
    if _collection_reinjected_count:
        session.testscollected += _collection_reinjected_count


@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session: pytest.Session) -> object:
    """Re-schedule session items just before execution begins.

    pytest-testmon may re-add previously-failed tests to ``session.items``
    *after* all ``pytest_collection_modifyitems`` hooks have finished.  When
    that happens the scheduler never sees them as destinations, so bridge
    tests are not injected and the state machine breaks.

    This hook acts as a safety net: it re-runs the scheduling logic on the
    final ``session.items`` list.  If the scheduler already ran successfully
    during collection (the common case), the item list already contains the
    correct bridges and this call is effectively a no-op — the re-scheduling
    simply rebuilds the same plan.
    """
    reinjected_count = _schedule_items(session.config, session.items)
    if reinjected_count:
        session.testscollected += reinjected_count
    return (yield)


def _count_reinjected_items(before: list[pytest.Item], after: list[pytest.Item]) -> int:
    """Return how many item objects were added to *after* that were not in *before*.

    Uses identity-based multiset comparison so duplicate objects are handled
    correctly if an item appears multiple times in an execution plan.
    """
    before_counts = Counter(id(item) for item in before)
    added = 0
    for item in after:
        item_id = id(item)
        if before_counts[item_id] > 0:
            before_counts[item_id] -= 1
        else:
            added += 1
    return added


def _schedule_items(config: pytest.Config, items: list[pytest.Item]) -> int:
    """Core scheduling logic shared by collection and pre-run hooks."""
    original_items = items[:]

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
        return 0

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
    return _count_reinjected_items(original_items, items)


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
    original_name = item.name
    item.name = f"[injected] {original_name}"
    # pytest exposes no public API to override the node ID; _nodeid is the
    # backing attribute for the read-only ``nodeid`` property.  This is a
    # known limitation: revisit if pytest removes or renames _nodeid.
    item._nodeid = f"[injected] {original_name}"


def _build_execution_plan(
    current_state: State,
    pure_clusters: dict[State, list[pytest.Item]],
    selected_transitions: dict[StateTransition, list[pytest.Item]],
    all_transitions: dict[StateTransition, list[pytest.Item]],
    full_graph: StateGraph,
) -> list[pytest.Item]:
    r"""Build an ordered item list using backtracking with memoization and cycle detection.

    **Algorithm Overview**

    The scheduler uses exhaustive backtracking to reorder user-selected tests
    and automatically inject bridging transitions needed to satisfy state constraints.

    **Phase 1: Early Exits (O(states))**
    - Check for unconnected nodes: LogWarning if any states are unreachable from current state.
    - Run any pure tests already reachable at the current state (free destinations).
    - Mark those tests as scheduled so they won't be reordered.

    **Phase 2: Backtracking Search (O(destinations^destinations) worst-case)**
    - Recursively explore different orderings of remaining destinations.
    - For each remaining destination state:
      * Use Dijkstra to find the shortest path from current state (O(edges log nodes)).
      * If reachable: create a branch, inject bridging tests, execute tests at that destination.
      * Recurse with new state and updated scheduled set.
      * If recursion succeeds: return the complete plan.
      * If recursion fails (returns None): backtrack and try the next destination.
    - If all orderings fail: raise _UnreachableStateError.

        **Optimization: Dead-End Memoization & Cycle Detection**
        - Memo key: (current_state, frozenset(remaining_destinations)).
        - Dead-end memoization caches only unsatisfiable branches, so repeated visits
            can be pruned immediately.
        - Cycle detection uses an in-flight ``visiting`` set for the same key shape.
            If we re-enter a key that is currently being explored, we return ``None``
            to break recursion loops.
        - Combined effect: guarantees termination even when the graph contains cycles
            and at least one destination is unreachable.

        **Cycle & Connectivity Detection**
    - ``full_graph.unreachable_states(current_state)``: Returns states with no path from current_state.
    - Logged as a warning; if a destination is in that set, _UnreachableStateError is raised.
        - Cycle detection via ``visiting``: if (state, remaining) is re-entered while
            still in progress, that branch returns ``None``.

    **Destination Ordering**
    - Tries destinations in sorted order for determinism.
    - Backtracking ensures the first valid ordering is returned.
    - Multiple user-selected tests on the same edge (multiple item variants) all run,
      with bridging re-navigation between them.

    **Edge Cases**
    - Empty selection: returns empty plan.
    - Already at required state: runs tests immediately without bridges.
    - Isolated graph components: _UnreachableStateError raised before backtracking starts.
    - Cyclic paths: dead-end memoization + cycle detection ensures recursive search terminates.

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
        Ordered list of pytest items forming a valid execution plan that
        satisfies all state constraints encountered along the chosen path.

    Raises:
        _UnreachableStateError: If no ordering of destinations can be found
            that bridges all gaps from *current_state* using available transitions,
            or if a required state is unreachable from *current_state*.
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

    def _inject_bridge(
        path: list[tuple[StateTransition, pytest.Item]],
        plan: list[pytest.Item],
        scheduled: set[pytest.Item],
        injected_ids: set[int],
    ) -> None:
        """Inject one bridging transition item per edge along *path*.

        For each edge, always use a bridge-only transition from the full
        suite rather than consuming any user-selected transition tests.
        This ensures that selected transition tests are only scheduled via
        ``_run_selected_at`` when their destination state is being targeted,
        so that all pure tests at intermediate states can run before any
        selected transition out of those states.
        Bridge-only transitions are NOT added to ``scheduled``, so the same
        bridging test can be injected again if the scheduler needs to cross
        the same edge multiple times (e.g. when two selected tests share an
        edge and each needs a fresh environment).

        Injected item IDs are recorded in *injected_ids* rather than applied
        immediately. ``_mark_as_injected`` must only be called after the
        backtracking search has committed to a final plan; calling it inside
        a speculative branch permanently mutates the pytest item even if that
        branch is later abandoned.
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
                    injected_ids.add(id(bridge_item))
                    plan.append(bridge_item)

    # Keys are (current_state, remaining_destinations). We only memoize dead ends.
    dead_end_memo: set[tuple[State, frozenset[State]]] = set()
    # Keys currently on the recursion stack; used to break in-flight cycles.
    visiting: set[tuple[State, frozenset[State]]] = set()

    def _backtrack_search(
        current_state: State,
        current_plan: list[pytest.Item],
        scheduled: set[pytest.Item],
        injected_ids: set[int],
    ) -> tuple[list[pytest.Item], set[int]] | None:
        """Recursively search for a valid ordering of destinations using backtracking.

        Tries each reachable remaining destination. If a path leads to an
        unreachable state, backtracks and tries a different destination.

        Uses dead-end memoization to prune known-unsatisfiable branches and an
        in-flight cycle guard (``visiting``) to prevent infinite recursion.

        Returns ``(plan, injected_ids)`` when a valid plan is found, or ``None``
        if this branch is a dead end.  ``injected_ids`` is a per-branch copy so
        that abandoned branches cannot permanently mark pytest items as injected.
        """
        remaining_destinations = _unscheduled_destinations(scheduled)
        if not remaining_destinations:
            # All destinations have been scheduled; return the plan.
            return current_plan, injected_ids

        memo_key = (current_state, frozenset(remaining_destinations))

        # Already determined this branch is a dead end.
        if memo_key in dead_end_memo:
            return None

        # Detect cycles: if we're currently visiting this key, we're in a loop.
        if memo_key in visiting:
            logger.debug(
                f"Cycle detected at state '{current_state}' with remaining "
                f"destinations {sorted(remaining_destinations)}. Returning None."
            )
            return None

        visiting.add(memo_key)

        try:
            # Try each remaining destination.
            for target_state in sorted(remaining_destinations):
                raw_path = full_graph.shortest_path(current_state, target_state)
                if raw_path is None:
                    # Can't reach this destination from the current state; try another.
                    continue

                # Create a copy of the plan, scheduled set, and injected-IDs set for
                # this branch. injected_ids must be copied so that a failed branch
                # cannot permanently label items via _mark_as_injected.
                branch_plan = current_plan[:]
                branch_scheduled = scheduled.copy()
                branch_injected = injected_ids.copy()

                # Inject bridging transitions to reach the target.
                _inject_bridge(raw_path, branch_plan, branch_scheduled, branch_injected)

                # Run the user-selected items at this destination.
                new_state = _run_selected_at(target_state, branch_plan, branch_scheduled)

                # Recurse; remaining destinations are recomputed from unscheduled items.
                result = _backtrack_search(new_state, branch_plan, branch_scheduled, branch_injected)

                if result is not None:
                    # Found a valid complete path on this branch.
                    return result

                # This branch led to a dead end; backtrack and try the next destination.

            # No valid ordering found from this state.
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
        # Doing this inside speculative backtracking branches would permanently
        # mutate pytest items even when those branches are later abandoned.
        for item in plan:
            if id(item) in final_injected_ids:
                _mark_as_injected(item)

    return plan
