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

from bundle_builder.charm import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, Charm
from bundle_builder.charmhub import CharmhubClient


class CharmhubClientStub(CharmhubClient):
    charms: set[Charm]

    def __init__(self, *charms: Charm):
        self.charms = set(charms)

    # TODO(raul): remove type ignore in subsequent type checker PRs
    # TODO(raul): remove type ignore in subsequent type checker PRs # type: ignore
    def find_charms(self, *args, **kwargs):  # type: ignore
        if "provides" in kwargs:
            return {
                charm.name
                for charm in self.charms
                if any(
                    (endpoint.type == ENDPOINT_PROVIDES and endpoint.interface == kwargs["provides"])
                    for endpoint in charm.endpoints
                )
            }
        if "requires" in kwargs:
            return {
                charm.name
                for charm in self.charms
                if any(
                    (endpoint.type == ENDPOINT_REQUIRES and endpoint.interface == kwargs["requires"])
                    for endpoint in charm.endpoints
                )
            }
        return set()

    # TODO(raul): remove type ignore in subsequent type checker PRs
    # TODO(raul): remove type ignore in subsequent type checker PRs # type: ignore
    def charm_from_store(self, *args, **kwargs):  # type: ignore
        for charm in self.charms:
            if charm.name == kwargs.get("charm_name"):
                return charm
        return None
