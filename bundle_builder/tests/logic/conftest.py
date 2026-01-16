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

from functools import cache

from bundle_builder.charm import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, Charm
from bundle_builder.charmhub import CharmhubClient


class CharmhubClientStub(CharmhubClient):
    charms: set[Charm]

    def __init__(self, *charms: Charm):
        self.charms = set(charms)

    @cache
    def find_charms(
        self, provides: str | None = None, requires: str | None = None, platform: str | None = None
    ) -> frozenset[str]:
        _ = platform  # unused in stub
        if provides is not None:
            return frozenset(
                {
                    charm.name
                    for charm in self.charms
                    if any(
                        (endpoint.type == ENDPOINT_PROVIDES and endpoint.interface == provides)
                        for endpoint in charm.endpoints
                    )
                }
            )
        if requires is not None:
            return frozenset(
                {
                    charm.name
                    for charm in self.charms
                    if any(
                        (endpoint.type == ENDPOINT_REQUIRES and endpoint.interface == requires)
                        for endpoint in charm.endpoints
                    )
                }
            )
        return frozenset()

    @cache
    def charm_from_store(
        self,
        charm_name: str,
        ubuntu_arch: str,
        charm_channel: str | None = None,
        charm_revision: int | None = None,
        ubuntu_version: str | None = None,
    ) -> Charm:
        for charm in self.charms:
            if charm.name == charm_name:
                return charm
        raise KeyError(f"Charm {charm_name} not found in stub client")
