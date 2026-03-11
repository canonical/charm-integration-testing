# Copyright (C) 2026 Canonical Ltd

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

"""Duck-typing helpers for probe signal exceptions.

Probes are exec'd by juju-doctor in an arbitrary Python environment, so they
cannot import from this package.  Instead they define small inline exception
classes that carry structured attributes.  Bundle builder identifies them by
attribute inspection rather than isinstance().

Signal protocol
---------------
MissingRelation signal:   hasattr(e, "endpoint") and hasattr(e, "app")
  e.endpoint   — endpoint name that has no relation (str)
  e.app        — application name whose endpoint is unsatisfied (str)

Bundle builder looks up the interface for the endpoint from the Charm object
it already holds; probes never need to know or carry interface names.

ChannelMismatch signal:   hasattr(e, "app") and hasattr(e, "track")
  e.app        — application name that is on the wrong track (str)
  e.track      — track it should be on (str)

Note: ChannelMismatch is a superset of MissingRelation's attribute set only if
the probe also sets ``interface``, so always check ChannelMismatch first (it
requires ``track``, which MissingRelation never sets).
"""


def is_missing_relation(exc: BaseException) -> bool:
    """Return True if *exc* carries a MissingRelation signal."""
    return hasattr(exc, "endpoint") and hasattr(exc, "app") and not hasattr(exc, "track")


def is_channel_mismatch(exc: BaseException) -> bool:
    """Return True if *exc* carries a ChannelMismatch signal."""
    return hasattr(exc, "app") and hasattr(exc, "track")
