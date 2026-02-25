# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for scheduler/markers.py - StateMarker and read_state_marker()."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from test_suite.scheduler.markers import StateMarker, read_state_marker
from test_suite.scheduler.states import State


class TestReadStateMarker:
    class TestNoMarker:
        def test_returns_none_when_item_has_no_state_marker(self, make_item: Callable[..., pytest.Item]) -> None:
            # GIVEN an item with no state marker
            item = make_item("test_something")

            # WHEN read_state_marker is called
            result = read_state_marker(item)

            # THEN None is returned
            assert result is None

    class TestPureTest:
        def test_provides_defaults_to_requires(self, make_item: Callable[..., pytest.Item]) -> None:
            # GIVEN a pure test (no explicit provides)
            item = make_item("test_pure", requires=State.DEPLOYED)

            # WHEN parsed
            marker = read_state_marker(item)

            # THEN provides equals the single requires state
            assert marker is not None
            assert marker.requires == (State.DEPLOYED,)
            assert marker.provides == State.DEPLOYED

        def test_not_a_transition(self, make_item: Callable[..., pytest.Item]) -> None:
            # GIVEN a pure test
            item = make_item("test_pure", requires=State.DEPLOYED)

            # WHEN parsed
            marker = read_state_marker(item)

            # THEN is_transition is False
            assert marker is not None
            assert not marker.is_transition

        def test_bridge_only_defaults_to_false(self, make_item: Callable[..., pytest.Item]) -> None:
            # GIVEN a test with no explicit bridge_only
            item = make_item("test_pure", requires=State.DEPLOYED)

            # WHEN parsed
            marker = read_state_marker(item)

            # THEN bridge_only is False
            assert marker is not None
            assert marker.bridge_only is False

    class TestTransitionTest:
        def test_explicit_provides_creates_transition(self, make_item: Callable[..., pytest.Item]) -> None:
            # GIVEN a transition with distinct requires and provides
            item = make_item("test_deploy", requires=State.EMPTY_MODEL, provides=State.DEPLOYED)

            # WHEN parsed
            marker = read_state_marker(item)

            # THEN the marker reflects the transition
            assert marker is not None
            assert marker.requires == (State.EMPTY_MODEL,)
            assert marker.provides == State.DEPLOYED
            assert marker.is_transition

        def test_bridge_only_true_is_parsed(self, make_item: Callable[..., pytest.Item]) -> None:
            # GIVEN a bridge_only transition
            item = make_item(
                "test_helper",
                requires=State.DEPLOYED,
                provides=State.NEIGHBOR_ONLY,
                bridge_only=True,
            )

            # WHEN parsed
            marker = read_state_marker(item)

            # THEN bridge_only is True
            assert marker is not None
            assert marker.bridge_only is True

    class TestMultipleRequires:
        def test_list_of_states_with_explicit_provides(self, make_item: Callable[..., pytest.Item]) -> None:
            # GIVEN a test requiring multiple states passed as a list
            item = make_item(
                "test_multi",
                requires=[State.DEPLOYED, State.NEIGHBOR_ONLY],
                provides=State.NO_CONTROLLER,
            )

            # WHEN parsed
            marker = read_state_marker(item)

            # THEN both requires states are present and it is a transition
            assert marker is not None
            assert set(marker.requires) == {State.DEPLOYED, State.NEIGHBOR_ONLY}
            assert marker.provides == State.NO_CONTROLLER
            assert marker.is_transition

        def test_tuple_of_states_with_explicit_provides(self, make_item: Callable[..., pytest.Item]) -> None:
            # GIVEN a test requiring multiple states passed as a tuple
            item = make_item(
                "test_multi",
                requires=(State.DEPLOYED, State.NEIGHBOR_ONLY),
                provides=State.NO_CONTROLLER,
            )

            # WHEN parsed
            marker = read_state_marker(item)

            # THEN both requires states are present
            assert marker is not None
            assert set(marker.requires) == {State.DEPLOYED, State.NEIGHBOR_ONLY}

        def test_provides_may_equal_one_of_requires(self, make_item: Callable[..., pytest.Item]) -> None:
            # GIVEN a multi-requires test where provides matches one of the requires
            item = make_item(
                "test_idempotent",
                requires=[State.DEPLOYED, State.NEIGHBOR_ONLY],
                provides=State.DEPLOYED,
            )

            # WHEN parsed
            marker = read_state_marker(item)

            # THEN is_transition is False (provides IS in requires)
            assert marker is not None
            assert not marker.is_transition

    class TestErrors:
        @dataclass
        class Params:
            label: str
            marker_kwargs: dict[str, object]
            error_match: str

        test_cases = [
            Params(
                label="missing_requires",
                marker_kwargs={"provides": State.DEPLOYED},
                error_match="without 'requires='",
            ),
            Params(
                label="invalid_requires_string",
                marker_kwargs={"requires": "not_a_valid_state"},
                error_match="not a valid State",
            ),
            Params(
                label="invalid_provides_string",
                marker_kwargs={"requires": State.EMPTY_MODEL, "provides": "not_a_valid_state"},
                error_match="not a valid State",
            ),
            Params(
                label="multiple_requires_without_explicit_provides",
                marker_kwargs={"requires": [State.DEPLOYED, State.NEIGHBOR_ONLY]},
                error_match="multiple requires states but no 'provides='",
            ),
            Params(
                label="invalid_value_in_requires_list",
                marker_kwargs={"requires": [State.DEPLOYED, "bad_state"], "provides": State.EMPTY_MODEL},
                error_match="not a valid State",
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
        def test(self, params: Params, make_item: Callable[..., pytest.Item]) -> None:
            # GIVEN an item with a malformed state marker
            item = make_item("test_bad", **params.marker_kwargs)

            # WHEN read_state_marker is called
            # THEN a ValueError is raised with a message matching the expected pattern
            with pytest.raises(ValueError, match=params.error_match):
                read_state_marker(item)


class TestStateMarkerIsTransition:
    @dataclass
    class Params:
        label: str
        requires: tuple[State, ...]
        provides: State
        expected: bool

    test_cases = [
        Params(
            label="pure_when_provides_equals_single_requires",
            requires=(State.DEPLOYED,),
            provides=State.DEPLOYED,
            expected=False,
        ),
        Params(
            label="transition_when_provides_differs_from_single_requires",
            requires=(State.EMPTY_MODEL,),
            provides=State.DEPLOYED,
            expected=True,
        ),
        Params(
            label="transition_when_provides_not_in_multi_requires",
            requires=(State.DEPLOYED, State.NEIGHBOR_ONLY),
            provides=State.NO_CONTROLLER,
            expected=True,
        ),
        Params(
            label="pure_when_provides_is_one_of_multiple_requires",
            requires=(State.DEPLOYED, State.NEIGHBOR_ONLY),
            provides=State.DEPLOYED,
            expected=False,
        ),
        Params(
            label="pure_when_provides_is_second_of_multiple_requires",
            requires=(State.DEPLOYED, State.NEIGHBOR_ONLY),
            provides=State.NEIGHBOR_ONLY,
            expected=False,
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        # GIVEN a StateMarker with the specified requires and provides
        marker = StateMarker(requires=params.requires, provides=params.provides)

        # WHEN is_transition is read
        result = marker.is_transition

        # THEN it matches the expected value
        assert result == params.expected
