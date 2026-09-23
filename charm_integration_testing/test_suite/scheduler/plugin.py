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

import _pytest.outcomes
import pytest

from .graph import StateGraph, StateTransition
from .markers import StateMarker, read_state_marker
from .states import State

try:
    # Builtin on 3.11+; pytest depends on the "exceptiongroup" backport on 3.10.
    _BaseExceptionGroup: type[BaseException] = BaseExceptionGroup  # type: ignore[name-defined]
except NameError:  # pragma: no cover - only exercised on Python 3.10
    from exceptiongroup import BaseExceptionGroup as _BaseExceptionGroup  # type: ignore[import-not-found,no-redef]

logger = logging.getLogger(__name__)

#: State assumed when no ``--current-state`` flag is given.
_DEFAULT_CURRENT_STATE = State.NO_BUNDLE

# Every item collected before -k/-m filtering; used to build the full state graph.
_all_collected: list[pytest.Item] = []

# Object IDs already labelled as injected, so re-injecting a bridge item doesn't
# double-prefix its name.
_injected_item_ids: set[int] = set()

# Maps a duplicate's object ID (see _duplicate_item_for_repeat) back to its
# original item's ID, so per-occurrence logic can trace a duplicate to its source.
_duplicate_original_ids: dict[int, int] = {}

# First state-marked item that failed. Once set, all remaining state-marked
# tests are skipped as "environment unknown".
_failed_state_test: pytest.Item | None = None

# Runtime belief about the environment's actual state (updated as tests run,
# not assumed from the static plan). None means "unknown". A passing
# transition advances this to its provides state; a skip leaves it unchanged
# (see the skip convention documented in markers.py). Set from
# --current-state at the start of collection.
_current_state: State | None = None

# Full state graph and all known transition tests, keyed by edge, built once
# from every collected item (pre -k/-m). Used at runtime to find a bridging
# path when a skip leaves _current_state short of what's needed next.
_full_graph: StateGraph | None = None
_all_transitions: dict[StateTransition, list[pytest.Item]] = {}

# Counter giving each runtime-injected recovery bridge a unique nodeid.
_recovery_counter: int = 0

# Edges excluded from recovery search because every candidate test for them
# has already skipped (retrying would just skip again).
_skipped_transitions: set[StateTransition] = set()

# Per-edge object IDs of template items that have skipped at runtime. An edge
# moves into _skipped_transitions once all its candidates are recorded here.
_skipped_transition_item_ids: dict[StateTransition, set[int]] = {}


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
        "state_disabled: Exclude a conditionally unavailable state transition from scheduler planning.",
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


def _record_skipped_transition_candidate(edge: StateTransition, item: pytest.Item) -> None:
    """Record that *item*, one of possibly several candidates for *edge*, has skipped.

    Only excludes *edge* from future recovery searches once every candidate
    registered for it has skipped, since an untried one may still work.
    """
    original_id = _duplicate_original_ids.get(id(item), id(item))
    skipped_ids = _skipped_transition_item_ids.setdefault(edge, set())
    skipped_ids.add(original_id)
    candidates = _all_transitions.get(edge, [])
    if candidates and skipped_ids.issuperset(id(c) for c in candidates):
        _skipped_transitions.add(edge)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:  # type: ignore[misc]
    """Keep ``_current_state`` in sync with what actually happened, not the plan.

    * Failing at any phase makes the state unknown (``_current_state = None``);
      all subsequent state-marked tests are then skipped.
    * Passing at call time advances ``_current_state`` to ``marker.provides``.
      This applies to transitions and to the multi-``requires`` "pure" case
      where ``provides`` equals only one of several accepted states (see
      ``test_provides_may_equal_one_of_requires`` in ``test_markers.py``); for
      an ordinary single-``requires`` pure test it's a no-op.
    * A transition skipped via plain ``pytest.skip()`` (any phase) leaves
      ``_current_state`` unchanged, per the convention in ``markers.py`` that
      every skip check runs before any state-mutating action. So the state
      stays at ``requires``, unless the call phase already advanced it to
      ``provides`` before a later teardown-phase skip.
    * A skip caused by ``xfail`` is *not* covered by that convention - the
      test body ran and may have mutated the environment - so it's treated
      like a failure instead.

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
    if report.failed or (report.skipped and getattr(report, "wasxfail", None) is not None):
        _failed_state_test = item
        _current_state = None
        logger.error(
            "State-marked test %r failed: environment state is unknown.  "
            "All remaining state-marked tests will be skipped.",
            item.nodeid,
        )
    elif report.when == "call" and report.passed:
        # Advance even for a non-transition marker: a multi-requires "pure" marker
        # whose provides matches only one accepted state still genuinely moves the
        # environment there (see test_provides_may_equal_one_of_requires). No-op
        # for an ordinary single-requires pure test.
        _current_state = marker.provides
    elif report.skipped and marker.is_transition:
        candidate_recorded = False
        for req_state in marker.requires:
            if req_state == _current_state:
                _record_skipped_transition_candidate(
                    StateTransition(from_state=req_state, to_state=marker.provides), item
                )
                candidate_recorded = True
        if candidate_recorded:
            logger.warning(
                "State-marked transition test %r was skipped: environment remains at %r.  "
                "The scheduler will try to recover without retrying this transition candidate.",
                item.nodeid,
                _current_state.value,
            )
        else:
            # No candidate was satisfied by _current_state (pytest_runtest_setup's
            # own skip), so this test may still be retried later.
            logger.warning(
                "State-marked transition test %r was skipped: environment remains at %r.  "
                "The scheduler may still attempt this transition later if the environment "
                "reaches a state it requires.",
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
    global _skipped_transition_item_ids
    _all_collected.clear()
    _injected_item_ids.clear()
    _duplicate_original_ids.clear()
    _failed_state_test = None
    _current_state = None
    _full_graph = None
    _all_transitions = {}
    _recovery_counter = 0
    _skipped_transitions.clear()
    _skipped_transition_item_ids.clear()


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip state-marked tests whose required state was never reached.

    Called before each test's setup phase. Skips *item* when:

    * the environment state is unknown (a prior state-marked test failed), or
    * ``_current_state`` doesn't satisfy *item*'s ``requires``.
      ``pytest_runtest_protocol`` already tried to bridge this gap right
      before *item*; if it failed, *item* is skipped here and recovery is
      retried for whatever follows it.
    * *item* is a transition candidate that already skipped earlier in this
      run (``_skipped_transition_item_ids``), even if ``_current_state`` now
      satisfies its ``requires`` - the static plan can schedule the same
      underlying test more than once, and a later occurrence shouldn't retry
      a candidate recovery bridges are already barred from retrying.

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
    if marker.is_transition:
        original_id = _duplicate_original_ids.get(id(item), id(item))
        edge = StateTransition(from_state=_current_state, to_state=marker.provides)
        if original_id in _skipped_transition_item_ids.get(edge, set()):
            pytest.skip(
                f"Skipped: this test already skipped earlier in the run as a candidate for the "
                f"{_current_state.value!r} -> {marker.provides.value!r} transition and will not be retried."
            )


def _is_recoverable_reconciliation_failure(exc: BaseException) -> bool:
    """Whether *exc* should be treated as a recoverable state-machine failure.

    Ordinary exceptions and pytest's own ``Skipped``/``Failed`` outcomes
    qualify. ``pytest.exit()``'s ``Exit`` is excluded even though it derives
    from ``Exception``: it's a whole-run abort request, not a recoverable
    failure, and must propagate untouched.

    Returns ``False`` for any ``_BaseExceptionGroup`` too, since
    ``split()`` tests this predicate against group nodes themselves, not
    just leaves; returning ``True`` for a group would hide an ``Exit``
    nested inside it instead of letting ``split()`` descend into it.
    """
    if isinstance(exc, _BaseExceptionGroup):
        return False
    return isinstance(exc, (Exception, _pytest.outcomes.OutcomeException)) and not isinstance(
        exc, _pytest.outcomes.Exit
    )


def _handle_reconciliation_failure(item: pytest.Item, bridge_items: list[pytest.Item], exc: BaseException) -> None:
    """Treat a failed setup-stack reconciliation like any other unexpected recovery failure.

    See ``pytest_runtest_protocol``'s docstring for why this can fail
    outside pytest's normal per-item reporting flow, so no report was ever
    logged for it.
    """
    global _current_state, _failed_state_test
    logger.error(
        "Failed to reconcile pytest's setup stack while recovering towards %s: %s.  "
        "Environment state is now unknown; all remaining state-marked tests will be skipped.",
        [b.nodeid for b in bridge_items],
        exc,
    )
    # Bump session.testsfailed (pytest's own exit-status accounting) and mirror its
    # --maxfail handling, so a reconciliation failure isn't silently a successful exit.
    item.session.testsfailed += 1
    maxfail = item.session.config.getvalue("maxfail")
    if maxfail and item.session.testsfailed >= maxfail:
        item.session.shouldfail = f"stopping after {item.session.testsfailed} failures"
    _current_state = None
    _failed_state_test = item


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> None:  # type: ignore[misc]
    """Recover the state machine before *nextitem* runs, if a gap opened up.

    Runs after *item*'s full setup/call/teardown protocol. If
    ``_current_state`` no longer satisfies *nextitem*'s ``requires``
    (typically because an in-between transition test skipped instead of
    running), searches the full state graph for a bridging path and splices
    fresh bridge item(s) into ``session.items`` right before *nextitem*.

    If *nextitem* is itself a transition candidate, any ``requires`` state it
    has already skipped for earlier in the run is excluded before the bridge
    search (and before checking whether the environment already satisfies
    it) - otherwise ``pytest_runtest_setup`` would just skip it right back
    out, wasting the bridge or missing a still-viable alternate state. If no
    state remains after exclusion, nothing is injected.

    If no bridging path exists, nothing is injected: ``pytest_runtest_setup``
    skips *nextitem* when its turn comes, and this hook retries recovery for
    whatever follows it - so a run of several unreachable tests is skipped
    one at a time rather than all at once.

    Pytest has already torn *item* down using the *original* ``nextitem``
    (via ``SetupState.teardown_exact``), which may retain a collector scope
    only valid for that original ``nextitem``. If the bridge belongs to a
    different module, that scope is stale and pytest's own ``SetupState.setup``
    would assert on it, so ``teardown_exact`` is called again towards the
    bridge's first item to cut the stack down to what's actually shared.

    That second ``teardown_exact`` call can run retained fixture finalizers
    outside pytest's normal reporting flow. A raised failure there (including
    ``Skipped``/``Failed`` from ``pytest.skip()``/``pytest.fail()``) is
    treated like any other recovery failure: state becomes unknown, all
    remaining state-marked tests are skipped, and ``session.testsfailed`` is
    bumped so the exit status reflects it - instead of crashing the whole run
    with an unrelated ``INTERNALERROR`` or exiting successfully despite the
    failed cleanup.

    Abort-style exceptions (``KeyboardInterrupt``, ``SystemExit``, pytest's
    own ``Exit``, ...) are never treated as recoverable and always propagate
    untouched, including when wrapped in a ``BaseExceptionGroup`` (split
    defensively here even though ``teardown_exact`` only ever groups
    ordinary finalizer failures today).
    """
    yield
    if nextitem is None or _current_state is None or _full_graph is None:
        return
    try:
        marker = read_state_marker(nextitem)
    except ValueError:
        marker = None
    if marker is None:
        return  # Unmarked test.

    # Exclude requires-states nextitem already skipped for (as a transition
    # candidate) before checking whether the environment satisfies it or
    # searching for a bridge - otherwise pytest_runtest_setup would just skip
    # nextitem back out, wasting a bridge or missing a still-viable state.
    candidate_requires = marker.requires
    if marker.is_transition:
        original_id = _duplicate_original_ids.get(id(nextitem), id(nextitem))
        candidate_requires = tuple(
            requires_state
            for requires_state in marker.requires
            if original_id
            not in _skipped_transition_item_ids.get(StateTransition(requires_state, marker.provides), set())
        )

    if _current_state in candidate_requires:
        return  # The environment already satisfies a still-viable requires-state.

    if not candidate_requires:
        logger.warning(
            "No recovery path from state %r to any of %r: %r will be skipped (every candidate edge for "
            "this test already skipped earlier in the run).",
            _current_state.value,
            [s.value for s in marker.requires],
            nextitem.nodeid,
        )
        return

    bridge_items = _find_recovery_bridge(_current_state, candidate_requires)
    if bridge_items is None:
        logger.warning(
            "No recovery path from state %r to any of %r: %r will be skipped.",
            _current_state.value,
            [s.value for s in candidate_requires],
            nextitem.nodeid,
        )
        return

    session_items = item.session.items
    insert_at = session_items.index(item) + 1
    session_items[insert_at:insert_at] = bridge_items
    # Reporters size the run from testscollected (set at collection time), so
    # bump it to reflect the newly-injected items.
    item.session.testscollected += len(bridge_items)
    # The just-finished teardown assumed the original nextitem; reconcile pytest's
    # setup stack with what will actually run next (the bridge) instead.
    try:
        item.session._setupstate.teardown_exact(bridge_items[0])
    except _BaseExceptionGroup as excgroup:
        # teardown_exact only wraps ordinary Exception/Skipped/Failed finalizer
        # failures in a group; abort exceptions bypass it entirely. Split
        # defensively anyway and re-raise anything unrecoverable untouched.
        recoverable, unrecoverable = excgroup.split(  # type: ignore[attr-defined]
            _is_recoverable_reconciliation_failure
        )
        if unrecoverable is not None:
            raise unrecoverable
        exc: BaseException = recoverable if recoverable is not None else excgroup
        _handle_reconciliation_failure(item, bridge_items, exc)
        return
    except _pytest.outcomes.Exit:
        # A deliberate whole-run abort from a finalizer; must propagate untouched
        # even though Exit (unlike Skipped/Failed) derives from Exception.
        raise
    except (Exception, _pytest.outcomes.OutcomeException) as exc:
        # Also catches Skipped/Failed from pytest.skip()/pytest.fail() in a
        # finalizer, treated the same as any other finalizer failure here.
        _handle_reconciliation_failure(item, bridge_items, exc)
        return
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

    Returns ``None`` if no path exists (excluding fully-skipped edges, see
    ``_skipped_transitions``), or if the graph claims an edge exists with no
    registered transition test (shouldn't happen; the graph is built directly
    from registered items).

    Each edge's template item is duplicated rather than reused directly,
    since the same template may be injected more than once and reusing one
    ``pytest.Item`` object would produce duplicate nodeids (see
    ``_duplicate_item_for_repeat``). When an edge has multiple candidates, one
    that hasn't yet skipped at runtime is preferred over ``candidates[0]``.
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
        already_skipped = _skipped_transition_item_ids.get(transition, set())
        template_item = next((c for c in candidates if id(c) not in already_skipped), candidates[0])
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
        if item.get_closest_marker("state_disabled") is not None:
            continue
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
    global _skipped_transitions, _skipped_transition_item_ids
    _full_graph = full_graph
    _all_transitions = dict(all_transitions)
    _current_state = current_state
    _failed_state_test = None
    _recovery_counter = 0
    _skipped_transitions = set()
    _skipped_transition_item_ids = {}

    items[:] = [item for item in items if item.get_closest_marker("state_disabled") is None]

    # ------------------------------------------------------------------
    # 2. Partition the USER-SELECTED items (post -k filter) into marked
    #    and unmarked.  These are the destinations the scheduler must reach.
    # ------------------------------------------------------------------
    selected_marked: list[tuple[pytest.Item, StateMarker]] = []
    unmarked: list[pytest.Item] = []

    for item in items:
        if item.get_closest_marker("state_disabled") is not None:
            continue
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
    visually distinct in ``pytest -v`` output.  Safe to call more than once
    on the same item, or on a duplicate that already inherited the marker
    from its template (e.g. a template reused as a bridge more than once) --
    otherwise the prefix would stack up as ``[injected] [injected] ...``.
    """
    if id(item) in _injected_item_ids or item.get_closest_marker("injected") is not None:
        _injected_item_ids.add(id(item))
        return
    _injected_item_ids.add(id(item))
    item.add_marker(pytest.mark.injected)
    original_name = item.name
    item.name = f"[injected] {original_name}"
    # pytest exposes no public API to override the node ID; _nodeid backs the
    # read-only nodeid property (revisit if pytest renames/removes it). Only the
    # trailing test-name segment is prefixed - the module prefix before it must
    # be preserved since JUnit/Test Observer derive template_id from it (GH-947).
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

    A bridging transition test may be scheduled more than once for the same
    edge (see ``_inject_bridge``), and running the same ``pytest.Item``
    object twice produces two results sharing one nodeid - JUnit consumers
    (e.g. Test Observer) then compact them into a single test case, hiding
    one run (SQT-913 / GH-445).

    A shallow copy keeps the duplicate on the same module/class/fixtures as
    *item*, with its own ``name``/``nodeid`` suffixed by a ``[occurrence]``
    index (e.g. ``test_upgrade_charm[1]``). *base_name*/*base_nodeid* default
    to *item*'s current name/nodeid; callers that already relabeled *item* in
    place (``_disambiguate_repeated_items``) should pass the pre-relabeling
    values so the index isn't stacked twice (``test_foo[1][2]``).
    ``pytest.Function`` caches a fixture request bound to ``self``
    (``_initrequest``); re-running it lets the duplicate resolve/tear down
    its own fixtures instead of aliasing the original's.

    ``copy.copy`` only copies attribute references, so ``own_markers``,
    ``keywords``, ``stash`` (per-item pass/fail state, e.g.
    ``resource_tracking``), ``user_properties`` (JUnit/Test Observer
    metadata), and ``_report_sections`` (captured output) all need
    independent copies here - otherwise marking/reporting on the duplicate
    would mutate *item* too, and a duplicate of an already-run template would
    inherit stale metadata. ``keywords`` is rebuilt after relabeling (it
    seeds from the node's name at construction) and repopulated with *item*'s
    own entries.

    The duplicate's object ID is recorded in ``_duplicate_original_ids``,
    pointing back to *item*'s original ID, so later per-occurrence logic can
    trace a duplicate to its source.
    """
    duplicate = copy.copy(item)
    if hasattr(item, "own_markers"):
        duplicate.own_markers = list(item.own_markers)
    if hasattr(item, "stash"):
        duplicate.stash = type(item.stash)()
        # pytest.Node.__init__ aliases self._store = self.stash; copy.copy leaves
        # it pointing at item's original stash, so rebind it too.
        if hasattr(duplicate, "_store"):
            duplicate._store = duplicate.stash
    if hasattr(item, "user_properties"):
        duplicate.user_properties = []
    if hasattr(item, "_report_sections"):
        duplicate._report_sections = []
    _duplicate_original_ids[id(duplicate)] = _duplicate_original_ids.get(id(item), id(item))
    _label_occurrence(
        duplicate,
        base_name if base_name is not None else item.name,
        base_nodeid if base_nodeid is not None else item.nodeid,
        occurrence,
    )
    if hasattr(item, "keywords"):
        new_keywords = type(item.keywords)(duplicate)
        # No public API to enumerate a node's own keyword entries (mirrors the
        # _nodeid precedent above). Skip item's own (now stale) name entry; the
        # fresh mapping above already seeded *duplicate*'s current name.
        for key, value in getattr(item.keywords, "_markers", {}).items():
            if key != item.name:
                new_keywords[key] = value
        duplicate.keywords = new_keywords
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

    Uses exhaustive backtracking to reorder user-selected tests and inject
    bridging transitions needed to satisfy state constraints:

    1. Run any pure tests already reachable at ``current_state`` for free.
    2. Recursively try each remaining destination state in sorted order: find
       the shortest path via Dijkstra, inject bridging tests, run the
       destination's selected tests, and recurse. Backtrack on failure.
    3. Dead-end branches are memoized by ``(state, frozenset(remaining))`` so
       they aren't re-explored; an in-flight ``visiting`` set breaks cycles by
       returning ``None`` if the same key is re-entered mid-search.
    4. Raise ``_UnreachableStateError`` if no ordering bridges all gaps, or if
       ``full_graph.unreachable_states`` shows a destination is unreachable.

    Multiple user-selected tests on the same edge all run, with bridging
    re-navigation between them.

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
