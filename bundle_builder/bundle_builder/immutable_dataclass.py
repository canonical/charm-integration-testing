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
from functools import wraps
from typing import Any, Callable, Dict, get_type_hints

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
def make_lazy_property(private_name, method) -> Any:
    def prop(self) -> Any:
        value = getattr(self, private_name)
        if value is _UNINITIALIZED:
            value = method(self)
            object.__setattr__(self, private_name, value)
        return value

    return property(prop)


# custom attribute used by @immutable_dataclass to identify cached methods
_MARKED_AS_CACHED_METHOD = "_is_cached_method"

# Sentinel value to identify cache-misses, because `None` can be a valid value
_CACHE_MISS = object()


# Cached method backed by instance-level cache
# Meant for use with @immutable_dataclass
# Much like functools.cache, but at the instance level instead of global
def cached_method(func):
    setattr(func, _MARKED_AS_CACHED_METHOD, True)
    return func


# Wraps the method to cache results in the given field in the instance
def make_cached_method(cached_field_name, method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        cache = getattr(args[0], cached_field_name)
        cache_key = tuple(args[1:]) + tuple(kwargs.items())
        result = cache.get(cache_key, _CACHE_MISS)
        if result == _CACHE_MISS:
            result = method(*args, **kwargs)
            cache[cache_key] = result
        return result

    return wrapped


# Create an immutable dataclass using frozen=True
# and defaults slots=True
def immutable_dataclass(_cls=None, **dataclass_kwargs):
    def wrap(cls) -> dataclass:
        # Collect methods decorated as computed fields
        computed_fields = {}
        cached_methods = {}
        for name, val in cls.__dict__.items():
            if getattr(val, "_is_computed_property", False):
                computed_fields[name] = val
            elif getattr(val, _MARKED_AS_CACHED_METHOD, False):
                cached_methods[name] = val

        # Create a private slot for each computed field
        annotations = dict(getattr(cls, "__annotations__", {}))
        for name, method in computed_fields.items():
            # Modify the class in place to add the private field
            private_name = f"_{name}"
            annotations[private_name] = get_type_hints(method).get("return", Any)
            setattr(cls, name, make_lazy_property(private_name, method))
            setattr(cls, private_name, field(init=False, repr=False, hash=False, compare=False, default=_UNINITIALIZED))

        # Create a private slot for the cache of each computed field, similar to above
        for name, method in cached_methods.items():
            cached_field_name = f"_cached_{name}"
            annotations[cached_field_name] = Dict[tuple, Any]
            setattr(cls, name, make_cached_method(cached_field_name, method))
            setattr(
                cls, cached_field_name, field(init=False, repr=False, hash=False, compare=False, default_factory=dict)
            )

        # Update the class annotations
        cls.__annotations__ = annotations

        # Apply dataclass with requested options
        return dataclass(frozen=True, slots=True, **dataclass_kwargs)(cls)

    # Handle both @decorator and @decorator()
    if _cls is not None and isinstance(_cls, type):
        return wrap(_cls)

    return wrap
