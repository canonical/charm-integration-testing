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


from dataclasses import field
from typing import Any, Callable, get_type_hints

from pydantic.dataclasses import dataclass


# Computed property attribute
# Meant for use with @immutable_dataclass
# Computed at initialization once
def computed_property(func: Callable) -> Callable:
    func._is_computed_property = True
    return func


# Sentinel value for uninitialized computed fields
_UNINITIALIZED = object()


# Create a lazy property that computes its value once
def make_lazy_property(private_name, method):
    def prop(self):
        value = getattr(self, private_name)
        if value is _UNINITIALIZED:
            value = method(self)
            object.__setattr__(self, private_name, value)
        return value

    return property(prop)


# Create an immutable dataclass using frozen=True
# and defaults slots=True
def immutable_dataclass(_cls=None, **dataclass_kwargs):
    def wrap(cls):
        # Collect methods decorated as computed fields
        computed_fields = {}
        for name, val in cls.__dict__.items():
            if getattr(val, "_is_computed_property", False):
                computed_fields[name] = val

        # Create a private slot for each computed field
        annotations = dict(getattr(cls, "__annotations__", {}))
        for name, method in computed_fields.items():
            # Modify the class in place to add the private field
            private_name = f"_{name}"
            annotations[private_name] = get_type_hints(cls).get(name, Any)
            setattr(cls, name, make_lazy_property(private_name, method))
            setattr(cls, private_name, field(init=False, repr=False, hash=False, compare=False, default=_UNINITIALIZED))

        # Update the class annotations
        cls.__annotations__ = annotations

        # Apply dataclass with requested options
        return dataclass(frozen=True, slots=True, **dataclass_kwargs)(cls)

    # Handle both @decorator and @decorator()
    if _cls is not None and isinstance(_cls, type):
        return wrap(_cls)

    return wrap
