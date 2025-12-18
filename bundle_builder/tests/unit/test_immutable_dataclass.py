# Copyright (C) 2025 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


import time
from dataclasses import FrozenInstanceError

import pytest

from bundle_builder.immutable_dataclass import cached_method, computed_property, immutable_dataclass


@immutable_dataclass
class User:
    first: str
    last: str

    @computed_property
    def full_name(self) -> str:
        return f"{self.first} {self.last}"

    @cached_method
    def echo_with_time_stamp(self, message: str) -> tuple[str, float]:
        return message, time.time()


def test_computed_property_value() -> None:
    # WHEN a user with first and last names
    u = User(first="Ada", last="Lovelace")
    # THEN the computed full_name should concatenate them
    # TODO(raul): remove type ignore in subsequent type checker PRs
    assert u.full_name == "Ada Lovelace"  # type: ignore


def test_computed_property_type() -> None:
    # WHEN a user
    u = User(first="Grace", last="Hopper")
    # THEN the computed full_name should be a string
    assert isinstance(u.full_name, str)


def test_cached_method_is_executed_for_different_inputs() -> None:
    # GIVEN a frozen User dataclass
    u = User(first="Alan", last="Turing")
    # WHEN asked to echo something hello and bye
    hello = u.echo_with_time_stamp("hello")
    bye = u.echo_with_time_stamp("bye")
    # THEN echos input with timestamp
    assert hello[0] == "hello"
    assert bye[0] == "bye"


def test_cached_method_result_remains_same() -> None:
    # GIVEN a frozen User dataclass who echoed hello with timestamp
    u = User(first="Alan", last="Turing")
    response_1 = u.echo_with_time_stamp("hello")
    # WHEN asked to echo hello again
    response_2 = u.echo_with_time_stamp("hello")
    # THEN responds with the exact original response
    assert response_2 == response_1


def test_frozen_enforcement() -> None:
    # GIVEN a frozen User dataclass
    u = User(first="Alan", last="Turing")
    # WHEN attempting to mutate an attribute
    # THEN it should raise FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        u.first = "Charles"


# TODO(raul): remove type ignore in subsequent type checker PRs
@immutable_dataclass()  # type: ignore
class Measurement:
    meters: float

    @computed_property
    def centimeters(self) -> float:
        return self.meters * 100

    @computed_property
    def inches(self) -> float:
        return self.meters * 39.3701

    @cached_method
    def centimeters_with_prefix(self, name: str) -> str:
        return f"{name}: {self.centimeters:.2f} cm"

    @cached_method
    def inches_with_prefix(self, name: str) -> str:
        return f"{name}: {self.inches:.2f} inches"


def test_multiple_computed_fields() -> None:
    # WHEN a measurement in meters
    m = Measurement(meters=1.5)
    # THEN computed centimeters and inches should be correct
    # TODO(raul): remove type ignore in subsequent type checker PRs
    assert m.centimeters == 150.0  # type: ignore
    # TODO(raul): remove type ignore in subsequent type checker PRs
    assert round(m.inches, 2) == 59.06  # type: ignore


def test_multiple_cached_methods() -> None:
    # WHEN a measurement in meters
    m = Measurement(meters=1.5)
    # THEN centimeters and inches with prefix should be correct
    assert m.centimeters_with_prefix("measured") == "measured: 150.00 cm"
    assert m.inches_with_prefix("measured") == "measured: 59.06 inches"


@immutable_dataclass
class Thing:
    name: str
    initialized: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "initialized", True)

    @computed_property
    def name_len(self) -> int:
        return len(self.name)


def test_preserves_original_post_init() -> None:
    # WHEN a class with a custom __post_init__ and a computed field
    t = Thing(name="Widget")
    # THEN both the original __post_init__ and computed field should apply
    # TODO(raul): remove type ignore in subsequent type checker PRs
    assert t.name_len == 6  # type: ignore
    assert t.initialized is True


@immutable_dataclass
class Loose:
    foo: int

    @computed_property
    def bar(self):  # type: ignore[no-untyped-def]
        return self.foo * 2


def test_missing_type_hint_fallbacks_to_any() -> None:
    # WHEN a computed property without a return type annotation
    loose = Loose(foo=3)
    # THEN it should compute correctly and default to Any
    # TODO(raul): remove type ignore in subsequent type checker PRs
    assert loose.bar == 6  # type: ignore
    assert isinstance(loose.bar, int)


def test_decorator_usage_forms() -> None:
    # WHEN a class using @immutable_dataclass() with parentheses
    # TODO(raul): remove type ignore in subsequent type checker PRs
    @immutable_dataclass(order=True)  # type: ignore
    class A:
        x: int

        @computed_property
        def double(self) -> int:
            return self.x * 2

    # THEN the computed field should work
    a = A(x=10)
    # TODO(raul): remove type ignore in subsequent type checker PRs
    assert a.double == 20  # type: ignore

    # WHEN a class using @immutable_dataclass without parentheses
    @immutable_dataclass
    class B:
        y: int

        @computed_property
        def square(self) -> int:
            return self.y * self.y

    # THEN the computed field should also work
    b = B(y=5)
    # TODO(raul): remove type ignore in subsequent type checker PRs
    assert b.square == 25  # type: ignore


def test_cannot_overwrite_frozen() -> None:
    # WHEN class attempts to overwrite frozen
    # THEN raise exception
    with pytest.raises(TypeError):
        # TODO(raul): remove type ignore in subsequent type checker PRs
        @immutable_dataclass(frozen=True)  # type: ignore
        class A:
            pass


def test_cannot_overwrite_slots() -> None:
    # WHEN class attempts to overwrite slots
    # THEN raise exception
    with pytest.raises(TypeError):
        # TODO(raul): remove type ignore in subsequent type checker PRs
        @immutable_dataclass(slots=False)  # type: ignore
        class A:
            pass
