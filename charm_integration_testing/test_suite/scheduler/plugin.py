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

import copy
import logging
from collections import defaultdict

import pytest

from .graph import StateGraph, StateTransition
from .markers import StateMarker, read_state_marker
from .states import State

logger = logging.getLogger(__name__)

#: State assumed when no ``--current-state`` flag is given.
_DEFAULT_CURRENT_STATE = State.NO_BUNDLE

# All items collected by pytest before any -k/-m filtering.
# Populated by pytest_itemcollected; used by modifyitems to build the full graph.
_all_collected: list[pytest.Item] = []

# Tracks item object IDs that have already been labelled as injected, so that
# re-injecting the same bridge item a second time does not double-prefix its name.
_injected_item_ids: set[int] = set()

# Maps a duplicate's object ID (see ``_duplicate_item_for_repeat``) back to the
# object ID of the originating item it was copied from, so later per-occurrence
# logic (e.g. applying injected-labeling to only the injected occurrence) can
# still identify which scheduled item a duplicate came from.
_duplicate_original_ids: dict[int, int] = {}

# Set to the first transition item that fails at call-time.  Once non-None,
# all subsequent state-marked tests are skipped because the environment state
# is unknown. Pure test failures do NOT set this: they leave the state intact.
_failed_state_test: pytest.Item | None = None

# The scheduler's runtime belief about the environment's actual state, updated
# as tests execute rather than assumed from the static plan. ``None`` means
# "unknown" (a state-marked test failed; see ``_failed_state_test``). A
# transition test that passes advances this to its ``provides`` state; one
# that is skipped leaves it unchanged, since a skipped transition never ran.
# Set from ``--current-state`` at the start of collection.
_current_state: State | None = None

# The full state graph and every known transition test, keyed by edge, built
# once from ALL collected items (pre -k/-m filtering) in
# ``pytest_collection_modifyitems``. Reused at runtime to find a bridging path
# when a skipped transition leaves ``_current_state`` short of what the next
# planned test requires.
_full_graph: StateGraph | None = None
_all_transitions: dict[StateTransition, list[pytest.Item]] = {}

# Monotonically increasing counter used to give each runtime-injected recovery
# bridge a unique name/nodeid, even if the same underlying transition test is
# injected more than once in the same session (see ``_find_recovery_bridge``).
_recovery_counter: int = 0

# Edges whose transition test has already been skipped at runtime for the
# state it departed from. Runtime recovery excludes these when searching for
# a bridging path: retrying the exact same test that just skipped would only
# skip again (the same fixture/condition caused it), which would otherwise
# make the scheduler re-inject it forever chasing the same unreachable state.
_skipped_transitions: set[StateTransition] = set()


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
    """Keep ``_current_state`` in sync with what actually happened, not the plan.

    * A state-marked test failing at setup, call, or teardown time means the
      environment state is no longer known: a setup failure may leave it
      partially configured, and a teardown failure may leave it indeterminate.
      ``_current_state`` becomes ``None`` and all subsequent state-marked
      tests are skipped (``pytest_runtest_setup``) until the run ends.

    * A transition test passing at call time means the environment reached
      its ``provides`` state: ``_current_state`` advances accordingly.

    * A transition test being skipped means it never ran, so the environment
      never left its ``requires`` state: ``_current_state`` is left as-is.
      ``pytest_runtest_protocol`` uses this to try bridging to whatever the
      next planned test actually needs.

    Pure tests (``requires == provides``) never change ``_current_state``,
    whether they pass, fail*, or skip (*except that a failure still halts
    everything, since pure test failures can also leave things broken).
    Unmarked tests are never affected.
    """
    global _failed_state_test, _current_state
    outcome = yield
    if _current_state is None:
        return  # Already unknown; no need to re-check.
    report = outcome.get_result()
    try:
        marker = read_state_marker(item)
    except ValueError:
        marker = None
    if marker is None:
        return
    if report.failed:
        _failed_state_test = item
        _current_state = None
        logger.error(
            "State-marked test %r failed: environment state is unknown.  "
            "All remaining state-marked tests will be skipped.",
            item.nodeid,
        )
    elif report.when == "call" and report.passed and marker.is_transition:
        _current_state = marker.provides
    elif report.skipped and marker.is_transition:
        for req_state in marker.requires:
            if req_state == _current_state:
                _skipped_transitions.add(StateTransition(from_state=req_state, to_state=marker.provides))
        logger.warning(
            "State-marked transition test %r was skipped: environment remains at %r.  "
            "The scheduler will try to recover a path (avoiding this edge) to whatever the next test needs.",
            item.nodeid,
            _current_state.value,
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    """Reset module-level state so re-running pytest in the same process starts fresh.

    The globals below are populated during a session and must be cleared
    when the session ends; otherwise a second ``pytest.main()`` call in the
    same Python process (e.g. from a test harness) would see stale data from
    the previous run.
    """
    global _all_collected, _injected_item_ids, _duplicate_original_ids, _failed_state_test
    global _current_state, _full_graph, _all_transitions, _recovery_counter, _skipped_transitions
    _all_collected.clear()
    _injected_item_ids.clear()
    _duplicate_original_ids.clear()
    _failed_state_test = None
    _current_state = None
    _full_graph = None
    _all_transitions = {}
    _recovery_counter = 0
    _skipped_transitions.clear()


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip state-marked tests whose required state was never reached.

    Called before each test's setup phase. Skips *item* when:

    * the environment state is unknown (a prior state-marked test failed), or
    * the environment's actual current state (``_current_state``, which may
      differ from what the static plan assumed if an earlier transition was
      skipped) doesn't satisfy *item*'s ``requires``.  ``pytest_runtest_protocol``
      already tried to bridge this gap by injecting a recovery transition
      immediately before *item*; if that succeeded, ``_current_state`` will
      already match by the time this hook runs. If not, *item* is skipped here,
      and recovery is attempted again for whatever test follows it.

    Unmarked tests are never affected.
    """
    if item is _failed_state_test:
        return  # Don't skip the failing test itself; let it report naturally.
    try:
        marker = read_state_marker(item)
    except ValueError:
        marker = None
    if marker is None:
        return
    if _current_state is None:
        failed_nodeid = _failed_state_test.nodeid if _failed_state_test is not None else "<unknown>"
        pytest.skip(f"Skipped: state-marked test {failed_nodeid!r} failed: environment state is unknown.")
    if _current_state not in marker.requires:
        pytest.skip(
            f"Skipped: environment is at state {_current_state.value!r}, but this test requires one of "
            f"{[s.value for s in marker.requires]!r} and no recovery path could bridge the gap."
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> None:  # type: ignore[misc]
    """Recover the state machine before *nextitem* runs, if a gap opened up.

    Runs after *item*'s entire setup/call/teardown protocol has finished
    (the natural boundary between one item and the next in pytest's
    ``session.items`` loop). If ``_current_state`` no longer satisfies
    *nextitem*'s ``requires`` (typically because a transition test in
    between was skipped rather than run), this looks for a bridging path in
    the full state graph and, if one exists, splices freshly-built bridge
    test item(s) into ``session.items`` right after *item* - i.e. before
    *nextitem* - so the environment is corrected before *nextitem* starts.

    If no bridging path exists, nothing is injected: ``pytest_runtest_setup``
    will skip *nextitem* when its turn comes, and this hook runs again
    afterwards for whatever test follows *nextitem*, repeating the same
    recovery attempt against the new state of the plan. This way a run of
    several unreachable tests in a row is skipped one at a time rather than
    all at once, and any test further down the plan that the environment's
    actual (unchanged) state still happens to satisfy keeps running normally.
    """
    yield
    if nextitem is None or _current_state is None or _full_graph is None:
        return
    try:
        marker = read_state_marker(nextitem)
    except ValueError:
        marker = None
    if marker is None or _current_state in marker.requires:
        return  # Unmarked test, or the environment already satisfies it.

    bridge_items = _find_recovery_bridge(_current_state, marker.requires)
    if bridge_items is None:
        logger.warning(
            "No recovery path from state %r to any of %r: %r will be skipped.",
            _current_state.value,
            [s.value for s in marker.requires],
            nextitem.nodeid,
        )
        return

    session_items = item.session.items
    insert_at = session_items.index(item) + 1
    session_items[insert_at:insert_at] = bridge_items
    logger.warning(
        "Recovering state machine: injecting %s to bridge %r towards %r before %r.",
        [b.nodeid for b in bridge_items],
        _current_state.value,
        [s.value for s in marker.requires],
        nextitem.nodeid,
    )


def _shortest_path_to_any(
    graph: StateGraph,
    from_state: State,
    to_states: tuple[State, ...],
    avoid: frozenset[StateTransition] = frozenset(),
) -> list[tuple[StateTransition, pytest.Item]] | None:
    """Return the cheapest of the shortest paths from *from_state* to any of *to_states*."""
    best: list[tuple[StateTransition, pytest.Item]] | None = None
    best_cost: int | None = None
    for target in to_states:
        path = graph.shortest_path(from_state, target, avoid=avoid)
        if path is None:
            continue
        cost = sum(transition.cost for transition, _ in path)
        if best_cost is None or cost < best_cost:
            best, best_cost = path, cost
    return best


def _find_recovery_bridge(from_state: State, to_states: tuple[State, ...]) -> list[pytest.Item] | None:
    """Build fresh, uniquely-named bridge items for a path from *from_state* to *to_states*.

    Returns ``None`` if no path exists in the full state graph (excluding any
    edge whose transition test has already skipped once - see
    ``_skipped_transitions``), or if the graph claims an edge exists but no
    transition test was ever registered for it (should not happen in
    practice; the graph is built directly from registered items).

    Each edge's template item (the transition test registered for it) is
    duplicated rather than reused directly: the same template may need to be
    injected more than once across a run's recovery attempts, and reusing the
    same ``pytest.Item`` object produces duplicate nodeids (see
    ``_duplicate_item_for_repeat``).
    """
    global _recovery_counter
    assert _full_graph is not None
    path = _shortest_path_to_any(_full_graph, from_state, to_states, avoid=frozenset(_skipped_transitions))
    if path is None:
        return None
    bridge_items: list[pytest.Item] = []
    for transition, _graph_item in path:
        candidates = _all_transitions.get(transition)
        if not candidates:
            return None
        template_item = candidates[0]
        _recovery_counter += 1
        duplicate = _duplicate_item_for_repeat(
            template_item,
            occurrence=_recovery_counter,
            base_name=f"{template_item.name}(recovered)",
            base_nodeid=f"{template_item.nodeid}(recovered)",
        )
        _mark_as_injected(duplicate)
        bridge_items.append(duplicate)
    return bridge_items


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

    # Publish the graph, edge->item map, and starting state for runtime
    # recovery (see pytest_runtest_protocol) before any early return below,
    # so recovery works even when the user's selection is unmarked-only.
    global _full_graph, _all_transitions, _current_state, _failed_state_test, _recovery_counter
    global _skipped_transitions
    _full_graph = full_graph
    _all_transitions = dict(all_transitions)
    _current_state = current_state
    _failed_state_test = None
    _recovery_counter = 0
    _skipped_transitions = set()

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
    node ID's trailing test-name segment with ``[injected]`` so it is
    visually distinct in ``pytest -v`` output.  Calling this function more
    than once on the same item is safe.
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
    # Only the trailing test-name segment (after the last "::") is prefixed;
    # the file-path/module prefix before it must be preserved, since JUnit
    # XML/Test Observer derive the test's template_id from that prefix (GH-947).
    path_prefix, separator, _ = item._nodeid.rpartition("::")
    item._nodeid = f"{path_prefix}{separator}[injected] {original_name}"


def _label_occurrence(item: pytest.Item, base_name: str, base_nodeid: str, occurrence: int) -> None:
    """Suffix *item*'s name/nodeid with a ``[occurrence]`` index, e.g. ``test_foo[2]``.

    *base_name*/*base_nodeid* are the pre-existing (unsuffixed) name/nodeid to
    build from, so repeated calls on the same item don't stack multiple
    indices (e.g. ``test_foo[1][2]``).
    """
    item.name = f"{base_name}[{occurrence}]"
    item._nodeid = f"{base_nodeid}[{occurrence}]"


def _duplicate_item_for_repeat(
    item: pytest.Item, occurrence: int, base_name: str | None = None, base_nodeid: str | None = None
) -> pytest.Item:
    """Build an independent duplicate of *item* for its *occurrence*-th scheduled run.

    A bridging transition test may be scheduled more than once when the same
    edge must be crossed several times (see ``_inject_bridge``), which means
    the exact same ``pytest.Item`` object can appear multiple times in the
    final plan. Running one ``Item`` object twice produces two test results
    that share a single nodeid, and JUnit consumers (e.g. Test Observer)
    compact same-nodeid results into a single test case, hiding one of the
    runs (SQT-913 / GH-445).

    A shallow copy keeps the duplicate on the same module/class/fixtures as
    *item* while giving it its own ``name`` and ``nodeid``, distinguished by
    a ``[occurrence]`` index (e.g. ``test_upgrade_charm[1]``,
    ``test_upgrade_charm[2]``) so Test Observer shows a clean, structured
    naming scheme instead of an ad hoc ``(repeat N)`` suffix. *base_name*/
    *base_nodeid* default to *item*'s current name/nodeid, but callers that
    have already relabeled *item* in place (see ``_disambiguate_repeated_items``)
    should pass the pre-relabeling values explicitly so the index isn't
    stacked on top of an earlier one (e.g. ``test_foo[1][2]``). Real
    ``pytest.Function`` items cache a fixture request that refers back to
    ``self`` at construction time (``_initrequest``); the duplicate re-runs
    that step so it resolves and tears down its own fixtures instead of
    aliasing the original item's.

    The duplicate's object ID is recorded in ``_duplicate_original_ids``,
    pointing back to *item*'s original object ID (chasing through any prior
    duplication), so later per-occurrence logic can still identify which
    scheduled item a duplicate came from.
    """
    duplicate = copy.copy(item)
    _duplicate_original_ids[id(duplicate)] = _duplicate_original_ids.get(id(item), id(item))
    _label_occurrence(
        duplicate,
        base_name if base_name is not None else item.name,
        base_nodeid if base_nodeid is not None else item.nodeid,
        occurrence,
    )
    initrequest = getattr(duplicate, "_initrequest", None)
    if callable(initrequest):
        initrequest()
    return duplicate


def _disambiguate_repeated_items(plan: list[pytest.Item]) -> list[pytest.Item]:
    """Give every occurrence of a repeated scheduled Item a unique, structured nodeid.

    Items that are scheduled only once are left untouched. Items scheduled
    more than once have every occurrence - including the first - labeled with
    a ``[occurrence]`` index (e.g. ``test_upgrade_charm[1]``,
    ``test_upgrade_charm[2]``): the first occurrence is relabeled in place,
    and later occurrences are replaced with a distinct duplicate (see
    ``_duplicate_item_for_repeat``), so each scheduled run is reported as its
    own test case instead of being merged with the others.
    """
    total_occurrences: defaultdict[int, int] = defaultdict(int)
    for item in plan:
        total_occurrences[id(item)] += 1

    base_names: dict[int, str] = {}
    base_nodeids: dict[int, str] = {}
    occurrence_counts: defaultdict[int, int] = defaultdict(int)
    disambiguated: list[pytest.Item] = []
    for item in plan:
        if total_occurrences[id(item)] == 1:
            disambiguated.append(item)
            continue

        base_names.setdefault(id(item), item.name)
        base_nodeids.setdefault(id(item), item.nodeid)
        occurrence_counts[id(item)] += 1
        occurrence = occurrence_counts[id(item)]

        if occurrence == 1:
            _label_occurrence(item, base_names[id(item)], base_nodeids[id(item)], occurrence)
            disambiguated.append(item)
        else:
            disambiguated.append(
                _duplicate_item_for_repeat(item, occurrence, base_names[id(item)], base_nodeids[id(item)])
            )
    return disambiguated


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

    return _disambiguate_repeated_items(plan)
