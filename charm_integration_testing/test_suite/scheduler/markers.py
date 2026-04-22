# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Custom pytest marker helpers for the state-driven test scheduler.

A single marker, ``@pytest.mark.state``, is used to annotate every test:

* **Pure tests** declare only the state they need::

      @pytest.mark.state(requires="deployed")
      def test_something(): ...

  The test is assumed to leave the environment state unchanged.

* **Transition tests** additionally declare the state they produce::

      @pytest.mark.state(requires="empty_model", provides="deployed")
      def test_deploy(): ...

  The scheduler may automatically inject these tests into the execution plan
  to bridge gaps between states: in addition to running them as normal tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from .states import State

#: Name of the pytest marker used by the scheduler.
STATE_MARKER_NAME = "state"


@dataclass(frozen=True)
class StateMarker:
    """Parsed, validated representation of ``@pytest.mark.state(...)``."""

    requires: tuple[State, ...]
    """One or more environment states from which this test may depart.

    Most tests declare a single state.  Transition tests that can depart from
    multiple states (e.g. a controller-kill test valid in any state) declare
    all of them; the scheduler registers a separate graph edge per entry.
    """

    provides: State
    """The environment state left behind after the test succeeds.

    For pure tests this equals each member of :attr:`requires`; for
    transitions it differs.
    """

    bridge_only: bool = False
    """When ``True`` the test is never treated as a user-selected destination.

    The test is still registered in the full state graph and may be injected
    automatically as a bridge, but the scheduler silently ignores it as a
    destination even when the user selects it via ``-k``.  Use this for
    "helper" transitions that should only ever run as setup steps.
    """

    @property
    def is_transition(self) -> bool:
        """``True`` when this test moves the environment to a new state."""
        return self.provides not in self.requires


def read_state_marker(item: pytest.Item) -> StateMarker | None:
    """Extract and validate the :data:`STATE_MARKER_NAME` marker from a test item.

    Returns ``None`` if the test has no state marker.

    Raises:
        ValueError: If the marker is present but missing the ``requires`` keyword,
            or if either value is not a valid :class:`~.states.State` member.
    """
    marker = item.get_closest_marker(STATE_MARKER_NAME)
    if marker is None:
        return None

    raw_requires = marker.kwargs.get("requires")
    if raw_requires is None:
        raise ValueError(
            f"Test {item.nodeid!r} has @pytest.mark.state without 'requires='. "
            "Every state-annotated test must declare which state it needs."
        )

    # Normalise: accept a single State value or a list/tuple of State values.
    raw_requires_seq: list[object] = list(raw_requires) if isinstance(raw_requires, (list, tuple)) else [raw_requires]

    requires_list: list[State] = []
    for raw in raw_requires_seq:
        try:
            requires_list.append(State(raw))
        except ValueError:
            valid = ", ".join(s.value for s in State)
            raise ValueError(
                f"Test {item.nodeid!r} has @pytest.mark.state(requires={raw!r}) "
                f"which is not a valid State.  Valid values: {valid}"
            ) from None
    requires = tuple(requires_list)

    # 'provides' defaults to the single requires state for pure tests.
    # Tests with multiple requires states must always specify 'provides'
    # (they are necessarily transitions).
    if "provides" not in marker.kwargs:
        if len(requires) > 1:
            raise ValueError(
                f"Test {item.nodeid!r} has multiple requires states but no 'provides='.  "
                "A test with multiple requires states must be a transition: "
                "specify provides= explicitly."
            )
        raw_provides = raw_requires_seq[0]
    else:
        raw_provides = marker.kwargs["provides"]

    try:
        provides = State(raw_provides)
    except ValueError:
        valid = ", ".join(s.value for s in State)
        raise ValueError(
            f"Test {item.nodeid!r} has @pytest.mark.state(provides={raw_provides!r}) "
            f"which is not a valid State.  Valid values: {valid}"
        ) from None

    bridge_only = bool(marker.kwargs.get("bridge_only", False))

    return StateMarker(requires=requires, provides=provides, bridge_only=bridge_only)
