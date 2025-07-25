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


from dataclasses import FrozenInstanceError

import pytest

from bundle_builder.immutable_dataclass import computed_property, immutable_dataclass


@immutable_dataclass
class User:
    first: str
    last: str

    @computed_property
    def full_name(self) -> str:
        return f"{self.first} {self.last}"


def test_computed_property_value():
    # WHEN a user with first and last names
    u = User(first="Ada", last="Lovelace")
    # THEN the computed full_name should concatenate them
    assert u.full_name == "Ada Lovelace"


def test_computed_property_type():
    # WHEN a user
    u = User(first="Grace", last="Hopper")
    # THEN the computed full_name should be a string
    assert isinstance(u.full_name, str)


def test_frozen_enforcement():
    # GIVEN a frozen User dataclass
    u = User(first="Alan", last="Turing")
    # WHEN attempting to mutate an attribute
    # THEN it should raise FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        u.first = "Charles"


@immutable_dataclass()
class Measurement:
    meters: float

    @computed_property
    def centimeters(self) -> float:
        return self.meters * 100

    @computed_property
    def inches(self) -> float:
        return self.meters * 39.3701


def test_multiple_computed_fields():
    # WHEN a measurement in meters
    m = Measurement(meters=1.5)
    # THEN computed centimeters and inches should be correct
    assert m.centimeters == 150.0
    assert round(m.inches, 2) == 59.06


@immutable_dataclass
class Thing:
    name: str
    initialized: bool = False

    def __post_init__(self):
        object.__setattr__(self, "initialized", True)

    @computed_property
    def name_len(self):
        return len(self.name)


def test_preserves_original_post_init():
    # WHEN a class with a custom __post_init__ and a computed field
    t = Thing(name="Widget")
    # THEN both the original __post_init__ and computed field should apply
    assert t.name_len == 6
    assert t.initialized is True


@immutable_dataclass
class Loose:
    foo: int

    @computed_property
    def bar(self):  # No type annotation
        return self.foo * 2


def test_missing_type_hint_fallbacks_to_any():
    # WHEN a computed property without a return type annotation
    loose = Loose(foo=3)
    # THEN it should compute correctly and default to Any
    assert loose.bar == 6
    assert isinstance(loose.bar, int)


def test_decorator_usage_forms():
    # WHEN a class using @immutable_dataclass() with parentheses
    @immutable_dataclass(order=True)
    class A:
        x: int

        @computed_property
        def double(self) -> int:
            return self.x * 2

    # THEN the computed field should work
    a = A(x=10)
    assert a.double == 20

    # WHEN a class using @immutable_dataclass without parentheses
    @immutable_dataclass
    class B:
        y: int

        @computed_property
        def square(self):
            return self.y * self.y

    # THEN the computed field should also work
    b = B(y=5)
    assert b.square == 25


def test_cannot_overwrite_frozen():
    # WHEN class attempts to overwrite frozen
    # THEN raise exception
    with pytest.raises(TypeError):

        @immutable_dataclass(frozen=True)
        class A:
            pass


def test_cannot_overwrite_slots():
    # WHEN class attempts to overwrite slots
    # THEN raise exception
    with pytest.raises(TypeError):

        @immutable_dataclass(slots=False)
        class A:
            pass
