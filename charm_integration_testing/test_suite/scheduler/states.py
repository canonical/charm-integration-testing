# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Canonical environment states for the test scheduler.

Import :class:`State` wherever a state name is needed; in
``@pytest.mark.state(...)`` calls, in ``--current-state`` comparisons, and in
the graph's Dijkstra traversal.  Using the enum instead of raw strings
eliminates typos and makes valid states discoverable via IDE autocomplete.

Example::

    @pytest.mark.state(requires=State.DEPLOYED)
    def test_something(): ...

    @pytest.mark.state(requires=State.NO_BUNDLE, provides=State.EMPTY_MODEL)
    def test_build_bundle(): ...

    @pytest.mark.state(requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
    def test_deploy(): ...
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    """Every canonical environment state the scheduler knows about.

    Subclassing ``str`` lets enum members be compared directly to strings and
    passed to any API that expects a ``str``, which keeps compatibility with
    the ``--current-state`` CLI option (whose value arrives as a plain string).
    """

    NO_BUNDLE = "no_bundle"
    NO_CONTROLLER = "no_controller"
    NO_MODEL = "no_model"
    EMPTY_MODEL = "empty_model"
    DEPLOYED = "deployed"
    NEIGHBOR_ONLY = "neighbor_only"
