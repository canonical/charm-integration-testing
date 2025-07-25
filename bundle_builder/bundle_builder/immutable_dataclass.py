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
from types import MethodType
from typing import Any, Callable, get_type_hints

from pydantic.dataclasses import dataclass


# Computed property attribute
# Meant for use with @immutable_dataclass
# Computed at initialization once
def computed_property(func: Callable) -> Callable:
    func._is_computed_property = True
    return func


# Create an immutable dataclass using frozen=True
# and defaults slots=True
def immutable_dataclass(_cls=None, **dataclass_kwargs):
    def wrap(cls):
        computed_properties = {}
        annotations = dict(getattr(cls, "__annotations__", {}))

        # Collect methods decorated as computed fields
        for name, val in cls.__dict__.items():
            if not getattr(val, "_is_computed_property", False):
                continue

            computed_properties[name] = val

            # Add field to ensure slot is created
            annotations[name] = get_type_hints(cls).get(name, Any)
            setattr(cls, name, field(init=False, repr=False, hash=False, compare=False))

        cls.__annotations__ = annotations

        # Wrap or extend __post_init__
        orig_post_init = getattr(cls, "__post_init__", None)

        def __post_init__(self):
            # Call original post-init if defined
            if orig_post_init:
                MethodType(orig_post_init, self)()

            # Evaluate computed fields
            for name, method in computed_properties.items():
                value = method(self)
                object.__setattr__(self, name, value)

        cls.__post_init__ = __post_init__

        # Apply dataclass with requested options
        return dataclass(frozen=True, slots=True, **dataclass_kwargs)(cls)

    # Handle both @decorator and @decorator()
    if _cls is not None and isinstance(_cls, type):
        return wrap(_cls)

    return wrap
