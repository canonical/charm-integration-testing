# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures and helpers for scheduler unit tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from test_suite.scheduler import plugin as _plugin_module


class FakeItem:
    """Minimal pytest.Item substitute that carries real pytest markers.

    Supports the subset of the pytest.Item interface used by the scheduler:
    ``get_closest_marker``, ``add_marker``, ``name``, and ``_nodeid``.
    """

    def __init__(self, name: str, **state_marker_kwargs: object) -> None:
        self.name = name
        self._nodeid = f"fake_tests/{name}.py::{name}"
        # Markers added via add_marker, keyed by name for fast lookup.
        self._added_marks: dict[str, Any] = {}
        # Build a real pytest Mark so read_state_marker sees genuine kwargs.
        self._state_mark = pytest.mark.state(**state_marker_kwargs).mark if state_marker_kwargs else None

    @property
    def nodeid(self) -> str:
        """Matches the pytest.Item.nodeid interface used by the scheduler."""
        return self._nodeid

    def get_closest_marker(self, marker_name: str) -> Any:
        """Return a previously added marker or the state marker by name."""
        if marker_name in self._added_marks:
            return self._added_marks[marker_name]
        if marker_name == "state":
            return self._state_mark
        return None

    def add_marker(self, marker: Any) -> None:
        """Store *marker* so it can be retrieved by get_closest_marker."""
        name = getattr(marker, "name", None)
        if name is not None:
            self._added_marks[str(name)] = marker


@pytest.fixture()
def make_item() -> Callable[..., pytest.Item]:
    """Factory fixture returning FakeItem instances cast to pytest.Item.

    The cast is deliberate: FakeItem implements the scheduler's required
    subset of pytest.Item at runtime, and lets callers pass items directly
    to scheduler functions without type-ignore comments.

    Usage::

        def test_something(make_item):
            item = make_item("test_foo", requires=State.DEPLOYED)
    """

    def factory(name: str, **state_marker_kwargs: object) -> pytest.Item:
        return cast(pytest.Item, FakeItem(name, **state_marker_kwargs))

    return factory


@pytest.fixture(autouse=True)
def reset_injected_ids() -> Iterator[None]:
    """Clear all module-level plugin globals before and after every test.

    Prevents state leaking between unit tests that call the plugin hooks
    directly.
    """
    _plugin_module._injected_item_ids.clear()
    _plugin_module._all_collected.clear()
    _plugin_module._failed_state_test = None
    yield
    _plugin_module._injected_item_ids.clear()
    _plugin_module._all_collected.clear()
    _plugin_module._failed_state_test = None
